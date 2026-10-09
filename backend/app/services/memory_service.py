"""Conversation memory: what was said earlier in a conversation.

Three kinds of memory, kept apart on purpose:

* **Short-term (this module).** The last ``SESSION_MEMORY_TURNS`` messages of a
  conversation, so "what about him?" can be understood. Kept in Redis for speed (one
  list per session, expiring after ``SESSION_TTL_HOURS`` of quiet), with the
  ``messages`` table in PostgreSQL as the durable copy. If Redis is down or has
  forgotten a session, the messages are read from PostgreSQL and Redis is refilled.
* **Workflow state.** Everything one request's agents found and decided lives in the
  LangGraph checkpointer (app/graph/checkpointer.py), not here.
* **Long-term.** The shop's records themselves (cases, past orders, reminders) are
  its long-term memory; the agents read them through tools. Nothing else about a
  person is stored.

Only the message text is kept, never tool results or reasoning.
"""

import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.models import Message

logger = get_logger(__name__)


class ConversationMemory:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis | None,
        *,
        turns: int = 10,
        ttl_seconds: int = 24 * 3600,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._turns = max(1, turns)
        self._ttl = ttl_seconds

    @staticmethod
    def key(session_id: str) -> str:
        return f"orm_ai:session:{session_id}:messages"

    async def recent(self, session_id: str) -> list[dict[str, Any]]:
        """The last messages of the conversation, oldest first."""
        if self._redis is not None:
            try:
                cached = await self._redis.lrange(self.key(session_id), -self._turns, -1)
                if cached:
                    return [json.loads(item) for item in cached]
            except (RedisError, OSError, ValueError) as exc:
                logger.warning("memory_cache_unavailable", error=type(exc).__name__)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.id.desc())
                    .limit(self._turns)
                )
            ).all()
        messages = [
            {"role": str(row.role), "content": row.content, "workflow_id": row.workflow_id}
            for row in reversed(rows)
        ]
        if messages:
            await self._write_cache(session_id, messages, replace=True)
        return messages

    async def append(
        self, session_id: str, role: str, content: str, *, workflow_id: str | None = None
    ) -> None:
        """Save a message: PostgreSQL first (the durable copy), then Redis."""
        async with self._sessions() as session:
            session.add(
                Message(session_id=session_id, workflow_id=workflow_id, role=role, content=content)
            )
            await session.commit()
        message = {"role": role, "content": content, "workflow_id": workflow_id}
        await self._write_cache(session_id, [message], replace=False)

    async def _write_cache(
        self, session_id: str, messages: list[dict[str, Any]], *, replace: bool
    ) -> None:
        """replace=True loads the whole list; False adds to a list Redis already has.

        Adding uses RPUSHX, which does nothing when Redis has no list for the session
        (it expired, or Redis restarted). The next read then reloads the full list from
        PostgreSQL, instead of trusting a list that holds only the newest message.
        """
        if self._redis is None:
            return
        key = self.key(session_id)
        values = [json.dumps(m, ensure_ascii=False) for m in messages]
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                if replace:
                    pipe.delete(key)
                    pipe.rpush(key, *values)
                else:
                    pipe.rpushx(key, *values)
                pipe.ltrim(key, -self._turns, -1)
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            logger.warning("memory_cache_unavailable", error=type(exc).__name__)
