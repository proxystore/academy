from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import uuid
from typing import Any
from typing import Protocol

import redis
import redis.asyncio

if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
    from asyncio import Queue
    from asyncio import QueueEmpty
    from asyncio import QueueShutDown

    AsyncQueue = Queue
else:  # pragma: <3.13 cover
    # Use of queues here is isolated to a single thread/event loop so
    # we only need culsans queues for the backport of shutdown() agent
    from culsans import AsyncQueue
    from culsans import AsyncQueueEmpty as QueueEmpty
    from culsans import AsyncQueueShutDown as QueueShutDown
    from culsans import Queue

from academy.exception import BadEntityIdError
from academy.exception import ForbiddenError
from academy.exception import MailboxTerminatedError
from academy.exception import MessageTooLargeError
from academy.exchange.transport import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ErrorResponse
from academy.message import Message

logger = logging.getLogger(__name__)

KB_TO_BYTES = 1024


class MailboxBackend(Protocol):
    """Backend protocol for storing mailboxes on server."""

    async def check_mailbox(
        self,
        client: str | None,
        uid: EntityId,
    ) -> MailboxStatus:
        """Check if a mailbox exists, or is terminated.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.

        Returns:
            The mailbox status.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        ...

    async def create_mailbox(
        self,
        client: str | None,
        uid: EntityId,
        agent: tuple[str, ...] | None = None,
    ) -> None:
        """Create a mailbox is not exists.

        This method should be idempotent.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.
            agent: The agent_mro for behavior discovery.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """

    async def terminate(self, client: str | None, uid: EntityId) -> None:
        """Close a mailbox.

        For security, the manager should keep a gravestone so the same id
        cannot be re-registered.

        Args:
            client: Client making the request.
            uid: Mailbox id to close.

        Raises:
            ForbiddenError: If the client does not have the right permissions.

        """
        ...

    async def discover(
        self,
        client: str | None,
        agent: str,
        allow_subclasses: bool,
    ) -> list[AgentId[Any]]:
        """Find mailboxes of matching agent class.

        Args:
            client: Client making the request.
            agent: Agent class to search for.
            allow_subclasses: Include agents that inherit from the target.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        ...

    async def get(
        self,
        client: str | None,
        uid: EntityId,
        *,
        timeout: float | None = None,
    ) -> Message[Any]:
        """Get messages from a mailbox.

        Args:
            client: Client making the request.
            uid: Mailbox id to get messages.
            timeout: Time in seconds to wait for message.
                If None, wait indefinitely.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            TimeoutError: There was not message received during the timeout.
        """
        ...

    async def put(self, client: str | None, message: Message[Any]) -> None:
        """Put a message in a mailbox.

        Args:
            client: Client making the request.
            message: Message to put in mailbox.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            MessageTooLargeError: The message is larger than the message
                size limit for this exchange.
        """
        ...


class PythonBackend:
    """Mailbox backend using in-memory python data structures.

    Args:
        message_size_limit_kb: Maximum message size to allow.
    """

    def __init__(
        self,
        message_size_limit_kb: int = 1024,
    ) -> None:
        self._owners: dict[EntityId, str | None] = {}
        self._mailboxes: dict[EntityId, AsyncQueue[Message[Any]]] = {}
        self._terminated: set[EntityId] = set()
        self._agents: dict[AgentId[Any], tuple[str, ...]] = {}
        self._locks: dict[EntityId, asyncio.Lock] = {}
        self.message_size_limit = message_size_limit_kb * KB_TO_BYTES

    def _has_permissions(
        self,
        client: str | None,
        entity: EntityId,
    ) -> bool:
        return entity not in self._owners or self._owners[entity] == client

    async def check_mailbox(
        self,
        client: str | None,
        uid: EntityId,
    ) -> MailboxStatus:
        """Check if a mailbox exists, or is terminated.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.

        Returns:
            The mailbox status.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        if uid not in self._mailboxes:
            return MailboxStatus.MISSING
        elif not self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        async with self._locks[uid]:
            if uid in self._terminated:
                return MailboxStatus.TERMINATED
            else:
                return MailboxStatus.ACTIVE

    async def create_mailbox(
        self,
        client: str | None,
        uid: EntityId,
        agent: tuple[str, ...] | None = None,
    ) -> None:
        """Create a mailbox is not exists.

        This method should be idempotent.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.
            agent: The agent_mro for behavior discovery.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        if not self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        mailbox = self._mailboxes.get(uid, None)
        if mailbox is None:
            if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
                queue: AsyncQueue[Message[Any]] = Queue()
            else:  # pragma: <3.13 cover
                queue: AsyncQueue[Message[Any]] = Queue().async_q
            self._mailboxes[uid] = queue
            self._terminated.discard(uid)
            self._owners[uid] = client
            self._locks[uid] = asyncio.Lock()
            if agent is not None and isinstance(uid, AgentId):
                self._agents[uid] = agent
            logger.info('Created mailbox for %s', uid)

    async def terminate(self, client: str | None, uid: EntityId) -> None:
        """Close a mailbox.

        For security, the manager should keep a gravestone so the same id
        cannot be re-registered.

        Args:
            client: Client making the request.
            uid: Mailbox id to close.

        Raises:
            ForbiddenError: If the client does not have the right permissions.

        """
        if not self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        self._terminated.add(uid)
        mailbox = self._mailboxes.get(uid, None)
        if mailbox is None:
            return

        async with self._locks[uid]:
            messages = await _drain_queue(mailbox)
            for message in messages:
                if message.is_request():
                    error = MailboxTerminatedError(uid)
                    body = ErrorResponse(exception=error)
                    response = message.create_response(body)
                    with contextlib.suppress(Exception):
                        await self.put(client, response)

            mailbox.shutdown(immediate=True)
            logger.info('Closed mailbox for %s', uid)

    async def discover(
        self,
        client: str | None,
        agent: str,
        allow_subclasses: bool,
    ) -> list[AgentId[Any]]:
        """Find mailboxes of matching agent class.

        Args:
            client: Client making the request.
            agent: Agent class to search for.
            allow_subclasses: Include agents that inherit from the target.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        found: list[AgentId[Any]] = []
        for aid, agents in self._agents.items():
            if not self._has_permissions(client, aid):
                continue
            if aid in self._terminated:
                continue
            if agent == agents[0] or (allow_subclasses and agent in agents):
                found.append(aid)
        return found

    async def get(
        self,
        client: str | None,
        uid: EntityId,
        *,
        timeout: float | None = None,
    ) -> Message[Any]:
        """Get messages from a mailbox.

        Args:
            client: Client making the request.
            uid: Mailbox id to get messages.
            timeout: Time in seconds to wait for message.
                If None, wait indefinitely.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            TimeoutError: There was not message received during the timeout.
        """
        if not self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        try:
            queue = self._mailboxes[uid]
        except KeyError as e:
            raise BadEntityIdError(uid) from e
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except QueueShutDown:
            raise MailboxTerminatedError(uid) from None
        except asyncio.TimeoutError:
            # In Python 3.10 and older, asyncio.TimeoutError and TimeoutError
            # are different error types.
            raise TimeoutError(
                f'No message retrieved within {timeout} seconds.',
            ) from None

    async def put(self, client: str | None, message: Message[Any]) -> None:
        """Put a message in a mailbox.

        Args:
            client: Client making the request.
            message: Message to put in mailbox.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            MessageTooLargeError: The message is larger than the message
                size limit for this exchange.
        """
        if not self._has_permissions(client, message.dest):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        if sys.getsizeof(message.body) > self.message_size_limit:
            raise MessageTooLargeError()

        try:
            queue = self._mailboxes[message.dest]
        except KeyError as e:
            raise BadEntityIdError(message.dest) from e

        async with self._locks[message.dest]:
            try:
                await queue.put(message)
            except QueueShutDown:
                raise MailboxTerminatedError(message.dest) from None


async def _drain_queue(queue: AsyncQueue[Message[Any]]) -> list[Message[Any]]:
    items: list[Message[Any]] = []

    while True:
        try:
            item = queue.get_nowait()
        except (QueueShutDown, QueueEmpty):
            break
        else:
            items.append(item)
            queue.task_done()

    return items


_CLOSE_SENTINEL = b'<CLOSED>'


class RedisBackend:
    """Redis backend of mailboxes.

    Args:
        hostname: Host address of redis.
        port: Redis port.
        message_size_limit_kb: Maximum message size to allow.
        kwargs: Addition arguments to pass to redis session.
    """

    def __init__(
        self,
        hostname: str = 'localhost',
        port: int = 6379,
        *,
        message_size_limit_kb: int = 1024,
        **kwargs: dict[str, Any],
    ) -> None:
        self.message_size_limit = message_size_limit_kb * KB_TO_BYTES
        self._client = redis.asyncio.Redis(
            host=hostname,
            port=port,
            decode_responses=False,
            **kwargs,  # pragma: no cover
        )

    def _owner_key(self, uid: EntityId) -> str:
        return f'owner:{uid.uid}'

    def _active_key(self, uid: EntityId) -> str:
        return f'active:{uid.uid}'

    def _agent_key(self, uid: EntityId) -> str:
        return f'agent:{uid.uid}'

    def _queue_key(self, uid: EntityId) -> str:
        return f'queue:{uid.uid}'

    async def _has_permissions(
        self,
        client: str | None,
        entity: EntityId,
    ) -> bool:
        owner = await self._client.get(
            self._owner_key(entity),
        )
        return owner is None or owner == client

    async def check_mailbox(
        self,
        client: str | None,
        uid: EntityId,
    ) -> MailboxStatus:
        """Check if a mailbox exists, or is terminated.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.

        Returns:
            The mailbox status.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        if not await self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        status = await self._client.get(self._active_key(uid))
        if status is None:
            return MailboxStatus.MISSING
        elif status == MailboxStatus.TERMINATED.value:
            return MailboxStatus.TERMINATED
        else:
            return MailboxStatus.ACTIVE

    async def create_mailbox(
        self,
        client: str | None,
        uid: EntityId,
        agent: tuple[str, ...] | None = None,
    ) -> None:
        """Create a mailbox is not exists.

        This method should be idempotent.

        Args:
            client: Client making the request.
            uid: Mailbox id to check.
            agent: The agent_mro for behavior discovery.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
        """
        if not await self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        await self._client.set(
            self._active_key(uid),
            MailboxStatus.ACTIVE.value,
        )

        if agent is not None:
            await self._client.set(
                self._agent_key(uid),
                ','.join(agent),
            )

        await self._client.set(
            self._owner_key(uid),
            client,
        )

    async def terminate(self, client: str | None, uid: EntityId) -> None:
        """Close a mailbox.

        For security, the manager should keep a gravestone so the same id
        cannot be re-registered.

        Args:
            client: Client making the request.
            uid: Mailbox id to close.

        Raises:
            ForbiddenError: If the client does not have the right permissions.

        """
        if not await self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        if await self.check_mailbox(client, uid) == MailboxStatus.MISSING:
            return

        await self._client.set(
            self._active_key(uid),
            MailboxStatus.TERMINATED.value,
        )

        pending = await self._client.lrange(self._queue_key(uid), 0, -1)
        await self._client.delete(self._queue_key(uid))
        # Sending a close sentinel to the queue is a quick way to force
        # the entity waiting on messages to the mailbox to stop blocking.
        # This assumes that only one entity is reading from the mailbox.
        await self._client.rpush(self._queue_key(uid), _CLOSE_SENTINEL)
        if isinstance(uid, AgentId):
            await self._client.delete(self._agent_key(uid))

        for raw in pending:
            if raw == _CLOSE_SENTINEL:
                break

            message: Message[Any] = Message.model_deserialize(raw)
            if message.is_request():
                error = MailboxTerminatedError(uid)
                body = ErrorResponse(exception=error)
                response = message.create_response(body)
                with contextlib.suppress(Exception):
                    await self.put(client, response)

    async def discover(
        self,
        client: str | None,
        agent: str,
        allow_subclasses: bool,
    ) -> list[AgentId[Any]]:
        """Find mailboxes of matching agent class.

        Args:
            client: Client making the request.
            agent: Agent class to search for.
            allow_subclasses: Include agents that inherit from the target.
        """
        found: list[AgentId[Any]] = []
        async for key in self._client.scan_iter(
            'agent:*',
        ):  # pragma: no branch
            mro_str = await self._client.get(key)
            assert isinstance(mro_str, str)
            mro = mro_str.split(',')
            if agent == mro[0] or (allow_subclasses and agent in mro):
                aid: AgentId[Any] = AgentId(uid=uuid.UUID(key.split(':')[-1]))
                found.append(aid)

        active: list[AgentId[Any]] = []
        for aid in found:
            if await self._has_permissions(client, aid):
                status = await self._client.get(self._active_key(aid))
                if status == MailboxStatus.ACTIVE.value:  # pragma: no branch
                    active.append(aid)

        return active

    async def get(
        self,
        client: str | None,
        uid: EntityId,
        *,
        timeout: float | None = None,
    ) -> Message[Any]:
        """Get messages from a mailbox.

        Args:
            client: Client making the request.
            uid: Mailbox id to get messages.
            timeout: Time in seconds to wait for message.
                If None, wait indefinitely.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            TimeoutError: There was not message received during the timeout.
        """
        if not await self._has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        _timeout = timeout if timeout is not None else 0
        status = await self._client.get(
            self._active_key(uid),
        )
        if status is None:
            raise BadEntityIdError(uid)
        elif status == MailboxStatus.TERMINATED.value:
            raise MailboxTerminatedError(uid)

        raw = await self._client.blpop(
            [self._queue_key(uid)],
            timeout=_timeout,
        )
        if raw is None:
            raise TimeoutError(
                f'Timeout waiting for next message for {uid} '
                f'after {timeout} seconds.',
            )

        # Only passed one key to blpop to result is [key, item]
        assert isinstance(raw, (tuple, list))
        assert len(raw) == 2  # noqa: PLR2004
        if raw[1] == _CLOSE_SENTINEL:  # pragma: no cover
            raise MailboxTerminatedError(uid)
        return Message.model_deserialize(raw[1])

    async def put(self, client: str | None, message: Message[Any]) -> None:
        """Put a message in a mailbox.

        Args:
            client: Client making the request.
            message: Message to put in mailbox.

        Raises:
            ForbiddenError: If the client does not have the right permissions.
            BadEntityIdError: The mailbox requested does not exist.
            MailboxTerminatedError: The mailbox is closed.
            MessageTooLargeError: The message is larger than the message
                size limit for this exchange.
        """
        if not await self._has_permissions(client, message.dest):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        status = await self._client.get(self._active_key(message.dest))
        if status is None:
            raise BadEntityIdError(message.dest)
        elif status == MailboxStatus.TERMINATED.value:
            raise MailboxTerminatedError(message.dest)
        else:
            serialized = message.model_serialize()
            if len(serialized) > self.message_size_limit:
                raise MessageTooLargeError()

            await self._client.rpush(
                self._queue_key(message.dest),
                serialized,
            )
