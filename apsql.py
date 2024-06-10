# Based on https://github.com/langchain-ai/langgraph/blob/main/langgraph/checkpoint/aiosqlite.py
import asyncio
import functools
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import AsyncIterator, Dict, Iterator, List, Optional, TypeVar

import psycopg
from langchain_core.runnables import ConfigurableFieldSpec, RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
)
from langgraph.checkpoint.sqlite import JsonPlusSerializerCompat, search_where
from typing_extensions import Self

T = TypeVar("T", bound=callable)


CheckpointUserId = ConfigurableFieldSpec(
    id="user_id",
    annotation=str,
    name="User ID",
    description=None,
    default="",
    is_shared=True,
)


class ModifiedCheckPointSaver(BaseCheckpointSaver):
    @property
    def config_specs(self) -> list[ConfigurableFieldSpec]:
        return super().config_specs + [CheckpointUserId]


def not_implemented_sync_method(func: T) -> T:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        raise NotImplementedError("The AsyncPostgresSaver does not support synchronous methods.")

    return wrapper


class AsyncPostgresSaver(ModifiedCheckPointSaver, AbstractAsyncContextManager):
    serde = JsonPlusSerializerCompat()
    conn: psycopg.AsyncConnection
    lock: asyncio.Lock
    is_setup: bool

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        *,
        serde: Optional[SerializerProtocol] = None,
    ):
        super().__init__(serde=serde)
        self.conn = conn
        self.lock = asyncio.Lock()
        self.is_setup = False

    @classmethod
    def from_conn_string(cls, conn_string: str) -> "AsyncPostgresSaver":
        conn = psycopg.AsyncConnection.connect(conninfo=conn_string)
        return AsyncPostgresSaver(conn=conn)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        __exc_type: Optional[type[BaseException]],
        __exc_value: Optional[BaseException],
        __traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        if self.is_setup:
            return await self.conn.close()

    @not_implemented_sync_method
    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Get a checkpoint tuple from the database.

        Note:
            This method is not implemented for the AsyncPostgresSaver. Use `aget` instead.
        """

    @not_implemented_sync_method
    def list(
        self,
        config: RunnableConfig,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from the database.

        Note:
            This method is not implemented for the AsyncPostgresSaver. Use `alist` instead.
        """

    @not_implemented_sync_method
    def search(
        self,
        metadata_filter: CheckpointMetadata,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """Search for checkpoints by metadata.

        Note:
            This method is not implemented for the AsyncPostgresSaver. Use `asearch` instead.
        """

    @not_implemented_sync_method
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> RunnableConfig:
        """Save a checkpoint to the database. FOO"""

    async def setup(self) -> None:
        """Set up the checkpoint database asynchronously.

        This method creates the necessary tables in the PostgreSQL database if they don't
        already exist. It is called automatically when needed and should not be called
        directly by the user.
        """
        async with self.lock:
            if self.is_setup:
                return
            self.conn = await self.conn
            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New Chat',
                    parent_ts TEXT,
                    checkpoint BYTEA,
                    metadata BYTEA,
                    timestamp TIMESTAMP,
                    not_useful BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (user_id, thread_id, thread_ts)
                );
                """
            )
            await self.conn.commit()
            self.is_setup = True

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Get a checkpoint tuple from the database asynchronously.

        This method retrieves a checkpoint tuple from the PostgreSQL database based on the
        provided config. If the config contains a "thread_ts" key, the checkpoint with
        the matching thread ID and timestamp is retrieved. Otherwise, the latest checkpoint
        for the given combination of user ID and thread ID is retrieved.

        Args:
            config (RunnableConfig): The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.
        """
        await self.setup()
        if config["configurable"].get("thread_ts"):
            async with await self.conn.execute(
                "SELECT checkpoint, parent_ts, metadata FROM checkpoints WHERE user_id = %s AND thread_id = %s AND thread_ts = %s",
                (
                    str(config["configurable"]["user_id"]),
                    str(config["configurable"]["thread_id"]),
                    str(config["configurable"]["thread_ts"]),
                ),
            ) as cursor:
                if value := await cursor.fetchone():
                    return CheckpointTuple(
                        config,
                        self.serde.loads(value[0]),
                        self.serde.loads(value[2]) if value[2] is not None else {},
                        (
                            {
                                "configurable": {
                                    "user_id": config["configurable"]["user_id"],
                                    "thread_id": config["configurable"]["thread_id"],
                                    "thread_ts": value[1],
                                }
                            }
                            if value[1]
                            else None
                        ),
                    )
        else:
            async with await self.conn.execute(
                "SELECT user_id, thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE user_id = %s AND thread_id = %s ORDER BY thread_ts DESC LIMIT 1",
                (
                    str(config["configurable"]["user_id"]),
                    str(config["configurable"]["thread_id"]),
                ),
            ) as cursor:
                if value := await cursor.fetchone():
                    return CheckpointTuple(
                        {
                            "configurable": {
                                "user_id": value[0],
                                "thread_id": value[1],
                                "thread_ts": value[2],
                            }
                        },
                        self.serde.loads(value[4]),
                        self.serde.loads(value[5]) if value[5] is not None else {},
                        (
                            {
                                "configurable": {
                                    "user_id": value[0],
                                    "thread_id": value[1],
                                    "thread_ts": value[3],
                                }
                            }
                            if value[3]
                            else None
                        ),
                    )

    async def alist(
        self,
        config: RunnableConfig,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List checkpoints from the database asynchronously.

        This method retrieves a list of checkpoint tuples from the PostgreSQL database based
        on the provided config. The checkpoints are ordered by timestamp in descending order.

        Args:
            config (RunnableConfig): The config to use for listing the checkpoints.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified timestamp are returned. Defaults to None.
            limit (Optional[int]): The maximum number of checkpoints to return. Defaults to None.

        Yields:
            AsyncIterator[CheckpointTuple]: An asynchronous iterator of checkpoint tuples.
        """
        await self.setup()
        query = (
            "SELECT user_id, thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE user_id = %s AND thread_id = %s ORDER BY thread_ts DESC"
            if before is None
            else "SELECT user_id, thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE user_id = %s AND thread_id = %s AND thread_ts < %s ORDER BY thread_ts DESC"
        )
        if limit:
            query += f" LIMIT {limit}"

        params = (
            (
                str(config["configurable"]["user_id"]),
                str(config["configurable"]["thread_id"]),
            )
            if before is None
            else (
                str(config["configurable"]["user_id"]),
                str(config["configurable"]["thread_id"]),
                str(before["configurable"]["thread_ts"]),
            )
        )

        async with await self.conn.execute(query, params) as cursor:
            async for user_id, thread_id, thread_ts, parent_ts, value, metadata in cursor:
                yield CheckpointTuple(
                    {"configurable": {"user_id": user_id, "thread_id": thread_id, "thread_ts": thread_ts}},
                    self.serde.loads(value),
                    self.serde.loads(metadata) if metadata is not None else {},
                    (
                        {
                            "configurable": {
                                "user_id": user_id,
                                "thread_id": thread_id,
                                "thread_ts": parent_ts,
                            }
                        }
                        if parent_ts
                        else None
                    ),
                )

    async def asearch(
        self,
        metadata_filter: CheckpointMetadata,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Search for checkpoints by metadata asynchronously.

        This method retrieves a list of checkpoint tuples from the PostgreSQL
        database based on the provided metadata filter. The metadata filter does
        not need to contain all keys defined in the CheckpointMetadata class.
        The checkpoints are ordered by timestamp in descending order.

        Args:
            metadata_filter (CheckpointMetadata): The metadata filter to use for searching the checkpoints.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified timestamp are returned. Defaults to None.
            limit (Optional[int]): The maximum number of checkpoints to return. Defaults to None.

        Yields:
            Iterator[CheckpointTuple]: An iterator of checkpoint tuples.
        """
        await self.setup()

        # construct query
        SELECT = "SELECT user_id, thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints "
        WHERE, params = search_where(metadata_filter, before)
        ORDER_BY = "ORDER BY thread_ts DESC "
        LIMIT = f"LIMIT {limit}" if limit else ""

        query = f"{SELECT}{WHERE}{ORDER_BY}{LIMIT}"

        # execute query
        async with await self.conn.execute(query, params) as cursor:
            async for user_id, thread_id, thread_ts, parent_ts, value, metadata in cursor:
                yield CheckpointTuple(
                    {"configurable": {"user_id": user_id, "thread_id": thread_id, "thread_ts": thread_ts}},
                    self.serde.loads(value),
                    self.serde.loads(metadata) if metadata is not None else {},
                    (
                        {
                            "configurable": {
                                "user_id": user_id,
                                "thread_id": thread_id,
                                "thread_ts": parent_ts,
                            }
                        }
                        if parent_ts
                        else None
                    ),
                )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> RunnableConfig:
        """Save a checkpoint to the database asynchronously.

        This method saves a checkpoint to the PostgreSQL database. The checkpoint is associated
        with the provided config and its parent config (if any).

        Args:
            config (RunnableConfig): The config to associate with the checkpoint.
            checkpoint (Checkpoint): The checkpoint to save.

        Returns:
            RunnableConfig: The updated config containing the saved checkpoint's timestamp.
        """
        await self.setup()
        async with await self.conn.execute(
            "INSERT INTO checkpoints (user_id, thread_id, thread_ts, parent_ts, checkpoint, metadata, timestamp) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, thread_id, thread_ts) DO UPDATE SET parent_ts = EXCLUDED.parent_ts, checkpoint = EXCLUDED.checkpoint, metadata = EXCLUDED.metadata",
            (
                str(config["configurable"]["user_id"]),
                str(config["configurable"]["thread_id"]),
                checkpoint["id"],
                config["configurable"].get("thread_ts"),
                self.serde.dumps(checkpoint),
                self.serde.dumps(metadata),
                checkpoint["ts"],
            ),
        ):
            await self.conn.commit()
        return {
            "configurable": {
                "user_id": config["configurable"]["user_id"],
                "thread_id": config["configurable"]["thread_id"],
                "thread_ts": checkpoint["id"],
            }
        }

    async def amark_checkpoint_as_not_useful(self, user_id: str, thread_id: str) -> int:
        """Mark a checkpoint as not useful in the database asynchronously.

        This method marks a checkpoint as not useful in the PostgreSQL database. The checkpoint
        is identified by the provided config.
        """
        await self.setup()
        checkpoint_tuple = await self.aget_tuple(
            config={
                "configurable": {
                    "user_id": user_id,
                    "thread_id": thread_id,
                }
            }
        )
        if not checkpoint_tuple:
            return
        async with await self.conn.execute(
            "UPDATE checkpoints SET not_useful = TRUE WHERE user_id = %s AND thread_id = %s AND thread_ts = %s",
            (
                user_id,
                thread_id,
                str(checkpoint_tuple.config["configurable"]["thread_ts"]),
            ),
        ) as result:
            await self.conn.commit()
            return result.rowcount

    async def aget_chats(self, user_id: str) -> List[Dict]:
        """
        Gets all the chats for a user. It returns a list of dictionaries (chats) with the user_id, thread_id, and title.
        """
        await self.setup()
        async with await self.conn.execute(
            "SELECT DISTINCT user_id, thread_id, title FROM checkpoints WHERE user_id = %s",
            (str(user_id),),
        ) as cursor:
            values = await cursor.fetchall()
            return [{"user_id": chat[0], "thread_id": chat[1], "title": chat[2]} for chat in values]

    async def aget_chat_state(self, user_id: str, thread_id: str, dictlike: bool = True) -> List[Dict]:
        await self.setup()
        latest_checkpoint = await self.aget_tuple(
            config={
                "configurable": {
                    "user_id": user_id,
                    "thread_id": thread_id,
                }
            }
        )
        if not latest_checkpoint:
            return []
        messages = [
            message
            for message in latest_checkpoint.checkpoint["channel_values"]["messages"]
            if message.type != "tool" and message.content.strip("user")
        ]
        if dictlike:
            messages = [
                {"role": "user" if message.type == "human" else "assistant", "message": message.content}
                for message in messages
            ]
        return messages

    async def arename_chat(self, user_id: str, thread_id: str, new_title: str) -> int:
        """
        Renames a chat. It returns the number of rows affected.
        """
        await self.setup()
        query = "UPDATE checkpoints SET title = %s WHERE user_id = %s AND thread_id = %s"
        params = (
            new_title,
            user_id,
            thread_id,
        )
        async with await self.conn.execute(query, params) as result:
            await self.conn.commit()
            return result.rowcount

    async def adelete_chat(self, user_id: str, thread_id: str) -> int:
        """
        Deletes a chat. It returns the number of rows affected.
        """
        await self.setup()
        query = "DELETE FROM checkpoints WHERE user_id = %s AND thread_id = %s"
        params = (
            user_id,
            thread_id,
        )
        async with await self.conn.execute(query, params) as result:
            await self.conn.commit()
            return result.rowcount