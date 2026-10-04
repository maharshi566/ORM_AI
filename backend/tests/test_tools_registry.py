"""The registry's guarantees: allowlists, validation, timeouts, retries, logging."""

import asyncio

from sqlalchemy import func, select

from app.models import PurchaseOrder, ToolCall
from app.tools.api_tools import FailureMode, FaultInjector
from app.tools.base import ToolContext, ToolInput, ToolOutput, ToolSpec
from app.tools.registry import ToolRegistry
from tests.conftest import STAFF


async def test_unknown_tool_is_an_error_not_a_crash(registry, make_ctx) -> None:
    result = await registry.call("delete_everything", {}, make_ctx(), agent="action")

    assert result.status == "error" and result.error_code == "unknown_tool"


async def test_agents_only_get_their_own_tools(registry, make_ctx) -> None:
    read = await registry.call(
        "get_product", {"product_id": "PRD-0001"}, make_ctx(), agent="investigation"
    )
    write = await registry.call("create_case", {}, make_ctx(), agent="data_retrieval")

    assert read.error_code == "forbidden"
    assert write.error_code == "forbidden"
    assert registry.schemas_for("investigation") == []
    assert len(registry.schemas_for("data_retrieval")) == 13
    assert len(registry.schemas_for("action")) == 7


async def test_arguments_are_validated_and_extra_fields_rejected(registry, make_ctx) -> None:
    missing = await registry.call("get_product", {}, make_ctx(), agent="data_retrieval")
    extra = await registry.call(
        "get_product",
        {"product_id": "PRD-0001", "shop_id": "SHOP-002"},
        make_ctx(),
        agent="data_retrieval",
    )

    assert missing.error_code == "invalid_input"
    # The shop always comes from the context, so an LLM cannot smuggle one in.
    assert extra.error_code == "invalid_input"


class _In(ToolInput):
    pass


class _Out(ToolOutput):
    pass


async def _slow(ctx: ToolContext, args: _In) -> _Out:
    await asyncio.sleep(5)
    return _Out()


async def _broken(ctx: ToolContext, args: _In) -> _Out:
    raise RuntimeError("secret detail from a bug")


async def test_timeouts_and_crashes_become_clean_errors(make_ctx) -> None:
    registry = ToolRegistry(
        [
            ToolSpec("slow", "slow", _In, _Out, _slow, kind="read", timeout_seconds=0.05),
            ToolSpec("broken", "broken", _In, _Out, _broken, kind="read"),
        ],
        agent_tools={"data_retrieval": ["slow", "broken"]},
        retry_backoff_seconds=0,
    )

    slow = await registry.call("slow", {}, make_ctx(), agent="data_retrieval")
    broken = await registry.call("broken", {}, make_ctx(), agent="data_retrieval")

    assert slow.error_code == "timeout" and slow.retryable
    assert broken.error_code == "internal_error" and not broken.retryable
    assert "secret" not in (broken.error_message or "")


async def test_read_tools_retry_temporary_failures(registry, make_ctx, seed_data) -> None:
    product_id = seed_data.edge_cases["negative_margin"]["product_id"]
    flaky = FaultInjector(FailureMode.SERVER_ERROR, fail_first=1)
    down = FaultInjector(FailureMode.SERVER_ERROR)

    recovered = await registry.call(
        "check_supplier_price",
        {"product_id": product_id},
        make_ctx("SHOP-004", faults=flaky),
        agent="data_retrieval",
    )
    failed = await registry.call(
        "check_supplier_price",
        {"product_id": product_id},
        make_ctx("SHOP-004", faults=down),
        agent="data_retrieval",
    )

    assert recovered.ok and recovered.attempts == 2
    assert failed.error_code == "upstream_error" and failed.attempts == 3  # 1 try + 2 retries


async def test_supplier_timeout_is_reported_as_timeout(registry, make_ctx, seed_data) -> None:
    product_id = seed_data.edge_cases["negative_margin"]["product_id"]
    hang = FaultInjector(FailureMode.TIMEOUT)
    spec = registry.spec("check_supplier_price")
    fast = ToolRegistry(
        [ToolSpec(**{**spec.__dict__, "timeout_seconds": 0.05})],
        agent_tools={"data_retrieval": [spec.name]},
        retry_backoff_seconds=0,
    )

    result = await fast.call(
        spec.name,
        {"product_id": product_id},
        make_ctx("SHOP-004", faults=hang),
        agent="data_retrieval",
    )

    assert result.error_code == "timeout" and result.attempts == 3


async def test_action_tools_are_never_retried_and_roll_back(
    registry, make_ctx, seed_data, session_factory
) -> None:
    product_id = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    ctx = make_ctx(approval=STAFF, faults=FaultInjector(FailureMode.SERVER_ERROR))

    result = await registry.call(
        "create_purchase_order",
        {
            "supplier_id": "SUP-002",
            "lines": [{"product_id": product_id, "quantity": 24}],
            "submit_to_supplier": True,
            "idempotency_key": "test:po:supplier-down",
        },
        ctx,
        agent="action",
    )

    assert result.error_code == "upstream_error" and result.attempts == 1
    async with session_factory() as session:
        saved = await session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.idempotency_key == "test:po:supplier-down")
        )
    assert saved is None  # the half-made order was rolled back


async def test_every_call_is_logged_to_tool_calls(registry, make_ctx, session_factory) -> None:
    await registry.call("get_low_stock_products", {}, make_ctx(), agent="data_retrieval")
    await registry.call("get_product", {"product_id": "NOPE-1"}, make_ctx(), agent="data_retrieval")

    async with session_factory() as session:
        rows = (await session.execute(select(ToolCall.tool_name, ToolCall.status))).all()
        total = await session.scalar(select(func.count()).select_from(ToolCall))
    assert total == 2
    assert ("get_product", "error") in [(name, str(status)) for name, status in rows]
