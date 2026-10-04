"""The tool registry: the single doorway through which agents call tools.

``ToolRegistry.call`` runs one tool call through the same steps every time:

1. Is the tool known, and is this agent allowed to use it? (allowlist below)
2. Do the arguments match the tool's input model? Unknown fields are rejected.
3. Run the tool with a timeout.
4. Read tools that fail with a temporary error (timeout, HTTP 500, 429) are retried
   with exponential backoff, up to their ``max_retries``. Action tools are never
   retried automatically.
5. Action tools commit on success and roll back on any failure. Read tools always
   roll back, so they release their connection and can never save a change.
6. Log the call (application log, plus the ``tool_calls`` table when a log
   session is available) and return a ``ToolResult``. It never raises.
"""

import asyncio
import random
import time
from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from app.core.logging import get_logger
from app.models import ToolCall
from app.tools.api_tools import API_TOOLS
from app.tools.base import ToolContext, ToolError, ToolErrorCode, ToolResult, ToolSpec
from app.tools.business_tools import BUSINESS_TOOLS
from app.tools.database_tools import DATABASE_TOOLS

logger = get_logger(__name__)

READ_TOOL_NAMES = [spec.name for spec in DATABASE_TOOLS + API_TOOLS]
ACTION_TOOL_NAMES = [spec.name for spec in BUSINESS_TOOLS]

# Which agent may call which tool. Agents not listed here get no tools at all, so
# the Supervisor, Triage, Investigation, Validator and Response agents can never
# fetch or change data themselves (they cannot invent a tool result either).
AGENT_TOOLS: dict[str, list[str]] = {
    "data_retrieval": READ_TOOL_NAMES,
    "action": ACTION_TOOL_NAMES,
    "knowledge": [],  # gets search_knowledge in Phase 3
}


class ToolRegistry:
    def __init__(
        self,
        specs: list[ToolSpec] | None = None,
        *,
        agent_tools: dict[str, list[str]] | None = None,
        retry_backoff_seconds: float = 0.3,
    ) -> None:
        all_specs = specs if specs is not None else DATABASE_TOOLS + API_TOOLS + BUSINESS_TOOLS
        self._specs = {spec.name: spec for spec in all_specs}
        self._agent_tools = agent_tools if agent_tools is not None else AGENT_TOOLS
        self._backoff = retry_backoff_seconds
        self._jitter = random.Random()  # noqa: S311 - jitter, not security

    def names(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def allowed(self, agent: str) -> list[str]:
        return [name for name in self._agent_tools.get(agent, []) if name in self._specs]

    def schemas_for(self, agent: str) -> list[dict[str, Any]]:
        """The function definitions an agent's LLM call receives (Phase 4)."""
        return [self._specs[name].json_schema() for name in self.allowed(agent)]

    async def call(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext, *, agent: str
    ) -> ToolResult:
        started = time.perf_counter()
        attempts = 0
        spec = self._specs.get(name)
        result: ToolResult
        if spec is None:
            result = self._error(
                name, ToolError(ToolErrorCode.UNKNOWN_TOOL, f"No tool named {name}.")
            )
        elif name not in self.allowed(agent):
            result = self._error(
                name,
                ToolError(ToolErrorCode.FORBIDDEN, f"The {agent} agent may not use {name}."),
            )
        else:
            try:
                args = spec.input_model.model_validate(arguments)
            except ValidationError as exc:
                result = self._error(
                    name,
                    ToolError(
                        ToolErrorCode.INVALID_INPUT,
                        "The arguments do not match the tool's input schema.",
                        details={"errors": jsonable_encoder(exc.errors(include_url=False))},
                    ),
                )
            else:
                result, attempts = await self._run(spec, args, ctx)

        result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        result.attempts = max(attempts, 1)
        logger.info(
            "tool_call",
            tool=name,
            agent=agent,
            shop_id=ctx.shop_id,
            workflow_id=ctx.workflow_id,
            status=result.status,
            error_code=result.error_code,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
        )
        await self._record(ctx, agent, name, arguments, result)
        return result

    async def _run(self, spec: ToolSpec, args: Any, ctx: ToolContext) -> tuple[ToolResult, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                output = await asyncio.wait_for(spec.handler(ctx, args), spec.timeout_seconds)
                output = spec.output_model.model_validate(output, from_attributes=True)
                if spec.kind == "action":
                    await ctx.session.commit()
                else:
                    # End the read transaction: frees the connection, and guarantees a
                    # read tool can never save a change, even by accident.
                    await ctx.session.rollback()
                data = output.model_dump(mode="json")
                return ToolResult(tool=spec.name, status="success", data=data), attempt
            except TimeoutError:
                error = ToolError(
                    ToolErrorCode.TIMEOUT,
                    f"{spec.name} did not finish within {spec.timeout_seconds}s.",
                )
            except ToolError as exc:
                error = exc
            except Exception as exc:  # a bug, not an expected failure
                logger.exception("tool_crashed", tool=spec.name, error_type=type(exc).__name__)
                error = ToolError(
                    ToolErrorCode.INTERNAL, "The tool failed unexpectedly.", retryable=False
                )
            await ctx.session.rollback()
            if error.retryable and spec.kind == "read" and attempt <= spec.max_retries:
                delay = self._backoff * (2 ** (attempt - 1)) * (1 + self._jitter.random() * 0.25)
                await asyncio.sleep(delay)
                continue
            return self._error(spec.name, error), attempt

    @staticmethod
    def _error(name: str, error: ToolError) -> ToolResult:
        return ToolResult(
            tool=name,
            status="error",
            error_code=error.code,
            error_message=error.message,
            error_details=jsonable_encoder(error.details) or None,
            retryable=error.retryable,
        )

    @staticmethod
    async def _record(
        ctx: ToolContext, agent: str, name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        if ctx.log_session_factory is None:
            return
        try:
            async with ctx.log_session_factory() as session:
                session.add(
                    ToolCall(
                        workflow_id=ctx.workflow_id,
                        agent=agent,
                        tool_name=name,
                        arguments=jsonable_encoder(arguments),
                        result=result.data if result.ok else result.error_details,
                        status="success" if result.ok else "error",
                        error_code=str(result.error_code) if result.error_code else None,
                        latency_ms=result.latency_ms,
                        created_at=ctx.now,
                    )
                )
                await session.commit()
        except Exception as exc:  # logging must never break the call itself
            logger.warning("tool_call_log_failed", tool=name, error=repr(exc))
