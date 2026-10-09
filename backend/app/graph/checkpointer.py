"""Saves the agent graph's state in the app's own database, after every step.

LangGraph calls this a checkpointer. With it, a workflow is not lost when the
process stops, and (Phase 5) a workflow can pause for a person's approval and resume
hours later from exactly where it stopped. ``graph.aget_state(config)`` reads a
workflow's latest state back, which the workflow API (Phase 6) shows.

Why not LangGraph's own PostgreSQL saver? It needs the psycopg driver, and psycopg
cannot run on the event loop that Windows uses by default (uvicorn without --reload,
and pytest), which is where this project is developed. This saver uses the same
SQLAlchemy + asyncpg engine as the rest of the app instead, so it works on Windows,
with Supabase's poolers, and on SQLite in the tests. Its storage layout and behaviour
follow LangGraph's InMemorySaver:

* ``graph_checkpoints``: one row per checkpoint, newest = highest checkpoint_id
  (LangGraph's IDs sort by time);
* ``graph_checkpoint_blobs``: each channel value, stored once per version, so an
  unchanged value is not copied into every checkpoint;
* ``graph_checkpoint_writes``: results of steps that finished while others in the
  same superstep did not (this is how a resumed workflow skips work already done).

Values are serialised by LangGraph's own serializer; these tables only hold bytes.
Only the async methods are implemented, because the app only runs the graph async.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GraphCheckpoint, GraphCheckpointBlob, GraphCheckpointWrite


def _config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }


class SQLCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._sessions = session_factory

    # String versions that always increase, exactly as InMemorySaver makes them.
    get_next_version = InMemorySaver.get_next_version

    # ------------------------------------------------------------ reading

    async def _tuple(self, session: AsyncSession, row: GraphCheckpoint) -> CheckpointTuple:
        checkpoint: Checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint))
        versions = {
            channel: str(version) for channel, version in checkpoint["channel_versions"].items()
        }
        values: dict[str, Any] = {}
        if versions:
            blobs = await session.scalars(
                select(GraphCheckpointBlob).where(
                    GraphCheckpointBlob.thread_id == row.thread_id,
                    GraphCheckpointBlob.checkpoint_ns == row.checkpoint_ns,
                    GraphCheckpointBlob.channel.in_(list(versions)),
                )
            )
            for blob in blobs:
                if versions.get(blob.channel) == blob.version and blob.value_type != "empty":
                    values[blob.channel] = self.serde.loads_typed((blob.value_type, blob.value))
        writes = await session.scalars(
            select(GraphCheckpointWrite)
            .where(
                GraphCheckpointWrite.thread_id == row.thread_id,
                GraphCheckpointWrite.checkpoint_ns == row.checkpoint_ns,
                GraphCheckpointWrite.checkpoint_id == row.checkpoint_id,
            )
            .order_by(GraphCheckpointWrite.id)
        )
        return CheckpointTuple(
            config=_config(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
            checkpoint={**checkpoint, "channel_values": values},
            metadata=self.serde.loads_typed((row.metadata_type, row.metadata_value)),
            parent_config=(
                _config(row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id)
                if row.parent_checkpoint_id
                else None
            ),
            pending_writes=[
                (w.task_id, w.channel, self.serde.loads_typed((w.value_type, w.value)))
                for w in writes
            ],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        query = select(GraphCheckpoint).where(
            GraphCheckpoint.thread_id == thread_id,
            GraphCheckpoint.checkpoint_ns == checkpoint_ns,
        )
        if checkpoint_id := get_checkpoint_id(config):
            query = query.where(GraphCheckpoint.checkpoint_id == checkpoint_id)
        else:
            query = query.order_by(GraphCheckpoint.checkpoint_id.desc()).limit(1)
        async with self._sessions() as session:
            row = await session.scalar(query)
            return await self._tuple(session, row) if row is not None else None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - LangGraph's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query = select(GraphCheckpoint)
        if config:
            query = query.where(GraphCheckpoint.thread_id == config["configurable"]["thread_id"])
            checkpoint_ns = config["configurable"].get("checkpoint_ns")
            if checkpoint_ns is not None:
                query = query.where(GraphCheckpoint.checkpoint_ns == checkpoint_ns)
            if checkpoint_id := get_checkpoint_id(config):
                query = query.where(GraphCheckpoint.checkpoint_id == checkpoint_id)
        if before and (before_id := get_checkpoint_id(before)):
            query = query.where(GraphCheckpoint.checkpoint_id < before_id)
        query = query.order_by(GraphCheckpoint.thread_id, GraphCheckpoint.checkpoint_id.desc())
        remaining = limit
        async with self._sessions() as session:
            for row in (await session.scalars(query)).all():
                if remaining is not None and remaining <= 0:
                    break
                if filter:
                    metadata = self.serde.loads_typed((row.metadata_type, row.metadata_value))
                    if not all(metadata.get(key) == value for key, value in filter.items()):
                        continue
                if remaining is not None:
                    remaining -= 1
                yield await self._tuple(session, row)

    # ------------------------------------------------------------ writing

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        stored = checkpoint.copy()
        values: dict[str, Any] = stored.pop("channel_values")  # type: ignore[misc]
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(stored)
        metadata_type, metadata_bytes = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        async with self._sessions() as session, session.begin():
            for channel, version in new_versions.items():
                value_type, value = (
                    self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")
                )
                await session.merge(
                    GraphCheckpointBlob(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        channel=channel,
                        version=str(version),
                        value_type=value_type,
                        value=value,
                    )
                )
            await session.merge(
                GraphCheckpoint(
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint["id"],
                    parent_checkpoint_id=config["configurable"].get("checkpoint_id"),
                    checkpoint_type=checkpoint_type,
                    checkpoint=checkpoint_bytes,
                    metadata_type=metadata_type,
                    metadata_value=metadata_bytes,
                )
            )
        return _config(thread_id, checkpoint_ns, checkpoint["id"])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        async with self._sessions() as session, session.begin():
            existing = {
                row.idx: row
                for row in await session.scalars(
                    select(GraphCheckpointWrite).where(
                        GraphCheckpointWrite.thread_id == thread_id,
                        GraphCheckpointWrite.checkpoint_ns == checkpoint_ns,
                        GraphCheckpointWrite.checkpoint_id == checkpoint_id,
                        GraphCheckpointWrite.task_id == task_id,
                    )
                )
            }
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                value_type, data = self.serde.dumps_typed(value)
                row = existing.get(idx)
                if row is not None:
                    if idx >= 0:
                        continue  # a normal write is saved once, as InMemorySaver does
                    row.channel, row.value_type, row.value = channel, value_type, data
                    row.task_path = task_path
                    continue
                session.add(
                    GraphCheckpointWrite(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        task_id=task_id,
                        idx=idx,
                        channel=channel,
                        value_type=value_type,
                        value=data,
                        task_path=task_path,
                    )
                )

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._sessions() as session, session.begin():
            for table in (GraphCheckpointWrite, GraphCheckpointBlob, GraphCheckpoint):
                await session.execute(delete(table).where(table.thread_id == thread_id))
