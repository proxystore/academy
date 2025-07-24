# ruff: noqa: D102
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import logging
import sys
import uuid
from collections.abc import Iterable
from typing import Any
from typing import Generic

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
    from asyncio import Queue
    from asyncio import QueueShutDown

    AsyncQueue = Queue
else:  # pragma: <3.13 cover
    # Use of queues here is isolated to a single thread/event loop so
    # we only need culsans queues for the backport of shutdown() agent
    from culsans import AsyncQueue
    from culsans import AsyncQueueShutDown as QueueShutDown
    from culsans import Queue

import redis.asyncio

from academy.agent import Agent
from academy.agent import AgentT
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange.factory import ExchangeFactory
from academy.exchange.redis import _MailboxState
from academy.exchange.redis import _RedisConnectionInfo
from academy.exchange.transport import _respond_pending_requests_on_terminate
from academy.exchange.transport import ExchangeTransportMixin
from academy.exchange.transport import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId
from academy.message import Message
from academy.serialize import NoPickleMixin
from academy.socket import address_by_hostname
from academy.socket import address_by_interface
from academy.socket import open_port
from academy.socket import SimpleSocketServer
from academy.socket import SocketClosedError
from academy.socket import SocketPool

logger = logging.getLogger(__name__)

_CLOSE_SENTINEL = b'<CLOSED>'
_THREAD_START_TIMEOUT = 5
_THREAD_JOIN_TIMEOUT = 5
_SERVER_ACK = b'<ACK>'
_SOCKET_POLL_TIMEOUT_MS = 50


@dataclasses.dataclass
class HybridAgentRegistration(Generic[AgentT]):
    """Agent registration for hybrid exchanges."""

    agent_id: AgentId[AgentT]
    """Unique identifier for the agent created by the exchange."""


class HybridExchangeTransport(ExchangeTransportMixin, NoPickleMixin):
    """Hybrid exchange transport bound to a specific mailbox."""

    def __init__(  # noqa: PLR0913
        self,
        mailbox_id: EntityId,
        redis_client: redis.asyncio.Redis,
        *,
        redis_info: _RedisConnectionInfo,
        namespace: str,
        host: str,
        port: int,
        interface: str | None = None,
    ) -> None:
        self._mailbox_id = mailbox_id
        self._redis_client = redis_client
        self._redis_info = redis_info
        self._namespace = namespace
        self._host = host
        self._port = port
        self._interface = interface

        self._address_cache: dict[EntityId, str] = {}
        if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
            self._messages: AsyncQueue[Message[Any]] = Queue()
        else:  # pragma: <3.13 cover
            self._messages: AsyncQueue[Message[Any]] = Queue().async_q
        self._socket_pool = SocketPool()
        self._started = asyncio.Event()
        self._shutdown = asyncio.Event()

        self._server = SimpleSocketServer(
            handler=self._direct_message_handler,
            host=host,
            port=port,
        )
        self._server_task = asyncio.create_task(
            self._run_direct_server(),
            name=f'hybrid-transport-direct-server-{self.mailbox_id}',
        )
        self._redis_task = asyncio.create_task(
            self._run_redis_listener(),
            name=f'hybrid-transport-redis-watcher-{self.mailbox_id}',
        )

    def _address_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:address:{uuid_to_base32(uid.uid)}'

    def _agent_key(self, aid: AgentId[Any]) -> str:
        return f'{self._namespace}:agent:{uuid_to_base32(aid.uid)}'

    def _status_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:status:{uuid_to_base32(uid.uid)}'

    def _queue_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:queue:{uuid_to_base32(uid.uid)}'

    @classmethod
    async def new(  # noqa: PLR0913
        cls,
        *,
        namespace: str,
        redis_info: _RedisConnectionInfo,
        interface: str | None = None,
        mailbox_id: EntityId | None = None,
        name: str | None = None,
        port: int | None = None,
    ) -> Self:
        """Instantiate a new transport.

        Args:
            namespace: Redis key namespace.
            redis_info: Redis connection information.
            interface: Network interface use for peer-to-peer communication.
                If `None`, the hostname of the local host is used.
            mailbox_id: Bind the transport to the specific mailbox. If `None`,
                a new user entity will be registered and the transport will be
                bound to that mailbox.
            name: Display name of the registered entity if `mailbox_id` is
                `None`.
            port: Port to listen for peer connection on.

        Returns:
            An instantiated transport bound to a specific mailbox.

        Raises:
            redis.exceptions.ConnectionError: If the Redis server is not
                reachable.
        """
        host = (
            address_by_interface(interface)
            if interface is not None
            else address_by_hostname()
        )
        port = port if port is not None else open_port()

        client = redis.asyncio.Redis(
            host=redis_info.hostname,
            port=redis_info.port,
            decode_responses=False,
            **redis_info.kwargs,
        )
        # Ensure the redis server is reachable else fail early
        await client.ping()

        if mailbox_id is None:
            mailbox_id = UserId.new(name=name)
            await client.set(
                f'{namespace}:status:{uuid_to_base32(mailbox_id.uid)}',
                _MailboxState.ACTIVE.value,
            )
            logger.info('Registered %s in exchange', mailbox_id)

        await client.set(
            f'{namespace}:address:{uuid_to_base32(mailbox_id.uid)}',
            f'{host}:{port}',
        )

        transport = cls(
            mailbox_id,
            client,
            redis_info=redis_info,
            namespace=namespace,
            interface=interface,
            host=host,
            port=port,
        )
        # Wait for the direct message server to start
        await asyncio.wait_for(transport._started.wait(), timeout=5)
        return transport

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    async def close(self) -> None:
        self._shutdown.set()
        self._messages.shutdown(immediate=True)
        await self._socket_pool.close()

        await asyncio.wait_for(self._server_task, timeout=5)

        self._redis_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._redis_task

        await self._redis_client.aclose()

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        found: list[AgentId[Any]] = []
        fqp = f'{agent.__module__}.{agent.__name__}'
        async for key in self._redis_client.scan_iter(  # pragma: no branch
            f'{self._namespace}:agent:*',
        ):
            mro_str = await self._redis_client.get(key)
            assert isinstance(mro_str, str)
            mro = mro_str.split(',')
            if fqp == mro[0] or (allow_subclasses and fqp in mro):
                aid: AgentId[Any] = AgentId(
                    uid=base32_to_uuid(key.split(':')[-1]),
                )
                found.append(aid)
        active: list[AgentId[Any]] = []
        for aid in found:
            status = await self._redis_client.get(self._status_key(aid))
            if status == _MailboxState.ACTIVE.value:  # pragma: no branch
                active.append(aid)
        return tuple(active)

    def factory(self) -> HybridExchangeFactory:
        return HybridExchangeFactory(
            redis_host=self._redis_info.hostname,
            redis_port=self._redis_info.port,
            redis_kwargs=self._redis_info.kwargs,
            interface=self._interface,
            namespace=self._namespace,
        )

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        try:
            return await asyncio.wait_for(
                self._messages.get(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f'Timeout waiting for next message for {self.mailbox_id} '
                f'after {timeout} seconds.',
            ) from None
        except QueueShutDown:
            raise MailboxTerminatedError(self.mailbox_id) from None

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> HybridAgentRegistration[AgentT]:
        aid: AgentId[AgentT] = AgentId.new(name=name)
        await self._redis_client.set(
            self._status_key(aid),
            _MailboxState.ACTIVE.value,
        )
        await self._redis_client.set(
            self._agent_key(aid),
            ','.join(agent._agent_mro()),
        )
        return HybridAgentRegistration(agent_id=aid)

    async def _send_direct(self, address: str, message: Message[Any]) -> None:
        await self._socket_pool.send(address, message.model_serialize())
        logger.debug(
            'Sent %s to %s via p2p at %s',
            type(message).__name__,
            message.dest,
            address,
        )

    async def send(self, message: Message[Any]) -> None:
        address = self._address_cache.get(message.dest, None)
        if address is not None:
            try:
                # This is as optimistic as possible. If the address of the
                # peer is cached, we assume the mailbox is still active and
                # the peer is still listening.
                await self._send_direct(address, message)
            except (SocketClosedError, OSError):
                # Our optimism let us down so clear the cache and try the
                # standard flow.
                self._address_cache.pop(message.dest)
            else:
                return

        status = await self._redis_client.get(self._status_key(message.dest))
        if status is None:
            raise BadEntityIdError(message.dest)
        elif status == _MailboxState.INACTIVE.value:
            raise MailboxTerminatedError(message.dest)

        maybe_address = await self._redis_client.get(
            self._address_key(message.dest),
        )
        try:
            # This branching is a little odd. We want to fall back to
            # Redis for message sending on two conditions: direct send fails
            # or no address was found. We raise a TypeError if no address
            # was found as a shortcut to get to the fall back.
            if isinstance(maybe_address, (bytes, str)):
                decoded_address = (
                    maybe_address.decode('utf-8')
                    if isinstance(maybe_address, bytes)
                    else maybe_address
                )
                await self._send_direct(decoded_address, message)
                self._address_cache[message.dest] = decoded_address
            else:
                raise TypeError('Did not active peer address in Redis.')
        except (TypeError, SocketClosedError, OSError):
            await self._redis_client.rpush(  # type: ignore[misc]
                self._queue_key(message.dest),
                message.model_serialize(),
            )
            logger.debug(
                'Sent %s to %s via redis',
                type(message).__name__,
                message.dest,
            )

    async def status(self, uid: EntityId) -> MailboxStatus:
        status = await self._redis_client.get(self._status_key(uid))
        if status is None:
            return MailboxStatus.MISSING
        elif status == _MailboxState.INACTIVE.value:
            return MailboxStatus.TERMINATED
        else:
            return MailboxStatus.ACTIVE

    async def terminate(self, uid: EntityId) -> None:
        # Warning: terminating a hybrid exchange mailbox is not guaranteed
        # to respond to all pending requests with a MailboxTerminatedError
        # when messages are sent directly.
        await self._redis_client.set(
            self._status_key(uid),
            _MailboxState.INACTIVE.value,
        )

        pending = await self._redis_client.lrange(self._queue_key(uid), 0, -1)  # type: ignore[misc]
        await self._redis_client.delete(self._queue_key(uid))
        # Sending a close sentinel to the queue is a quick way to force
        # the entity waiting on messages to the mailbox to stop blocking.
        # This assumes that only one entity is reading from the mailbox.
        await self._redis_client.rpush(self._queue_key(uid), _CLOSE_SENTINEL)  # type: ignore[misc]

        messages: list[Message[Any]] = [
            Message.model_deserialize(raw)
            for raw in pending
            if raw != _CLOSE_SENTINEL
        ]
        await _respond_pending_requests_on_terminate(messages, self)

        if isinstance(uid, AgentId):
            await self._redis_client.delete(self._agent_key(uid))

    async def _get_message_from_redis(self) -> None:
        # Block indefinitely with timeout=0
        raw = await self._redis_client.blpop(  # type: ignore[misc]
            [self._queue_key(self.mailbox_id)],
            timeout=0,
        )

        # Only passed one key to blpop to result is [key, item]
        assert isinstance(raw, (tuple, list))
        assert len(raw) == 2  # noqa: PLR2004
        if raw[1] == _CLOSE_SENTINEL:  # pragma: no cover
            self._shutdown.set()
            self._messages.shutdown(immediate=True)
            raise MailboxTerminatedError(self.mailbox_id)
        message: Message[Any] = Message.model_deserialize(raw[1])
        logger.debug(
            'Received %s to %s via redis',
            type(message).__name__,
            self.mailbox_id,
        )
        await self._messages.put(message)

    async def _run_redis_listener(self) -> None:
        logger.debug('Started redis listener task for %s', self.mailbox_id)
        try:
            while True:
                status = await self._redis_client.get(
                    self._status_key(self.mailbox_id),
                )
                if status is None:  # pragma: no cover
                    raise AssertionError(
                        f'Status for mailbox {self.mailbox_id} did not exist '
                        'in Redis server. This means that something '
                        'incorrectly deleted the key.',
                    )
                elif (
                    status == _MailboxState.INACTIVE.value
                ):  # pragma: no cover
                    break
                await self._get_message_from_redis()
        except MailboxTerminatedError:  # pragma: no cover
            pass
        except Exception:
            logger.exception(
                'Error in redis listener task for %s',
                self.mailbox_id,
            )
        finally:
            self._shutdown.set()
            self._messages.shutdown(immediate=True)
            logger.debug(
                'Stopped redis listener task for %s',
                self.mailbox_id,
            )

    async def _direct_message_handler(self, payload: bytes) -> bytes | None:
        message: Message[Any] = Message.model_deserialize(payload)
        logger.debug(
            'Received %s to %s via p2p',
            type(message).__name__,
            self.mailbox_id,
        )
        await self._messages.put(message)
        return None

    async def _run_direct_server(self) -> None:
        logger.debug(
            'Started direct message server task for %s',
            self.mailbox_id,
        )
        async with self._server.serve():
            self._started.set()
            await self._shutdown.wait()

            await self._redis_client.delete(
                self._address_key(self.mailbox_id),
            )


class HybridExchangeFactory(ExchangeFactory[HybridExchangeTransport]):
    """Hybrid exchange client factory.

    The hybrid exchange uses peer-to-peer communication via TCP and a
    central Redis server for mailbox state and queueing messages for
    offline entities.

    Args:
        redis_host: Redis server hostname.
        redis_port: Redis server port.
        redis_kwargs: Extra keyword arguments to pass to
            [`redis.Redis()`][redis.Redis].
        interface: Network interface use for peer-to-peer communication. If
            `None`, the hostname of the local host is used.
        namespace: Redis key namespace. If `None` a random key prefix is
            generated.
        ports: An iterable of ports to give each client a unique port from a
            user defined set. A StopIteration exception will be raised in
            `create_*_client()` methods if the number of clients in the process
            is greater than the length of the iterable.
    """

    def __init__(  # noqa: PLR0913
        self,
        redis_host: str,
        redis_port: int,
        *,
        redis_kwargs: dict[str, Any] | None = None,
        interface: str | None = None,
        namespace: str | None = 'default',
        ports: Iterable[int] | None = None,
    ) -> None:
        self._namespace = (
            namespace
            if namespace is not None
            else uuid_to_base32(uuid.uuid4())
        )
        self._interface = interface
        self._redis_info = _RedisConnectionInfo(
            redis_host,
            redis_port,
            redis_kwargs if redis_kwargs is not None else {},
        )
        self._ports = None if ports is None else iter(ports)

    async def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        registration: HybridAgentRegistration[Any] | None = None,  # type: ignore[override]
    ) -> HybridExchangeTransport:
        return await HybridExchangeTransport.new(
            interface=self._interface,
            mailbox_id=mailbox_id,
            name=name,
            namespace=self._namespace,
            port=None if self._ports is None else next(self._ports),
            redis_info=self._redis_info,
        )


def base32_to_uuid(uid: str) -> uuid.UUID:
    """Parse a base32 string as a UUID."""
    padding = '=' * ((8 - len(uid) % 8) % 8)
    padded = uid + padding
    uid_bytes = base64.b32decode(padded)
    return uuid.UUID(bytes=uid_bytes)


def uuid_to_base32(uid: uuid.UUID) -> str:
    """Encode a UUID as a trimmed base32 string."""
    uid_bytes = uid.bytes
    base32_bytes = base64.b32encode(uid_bytes).rstrip(b'=')
    return base32_bytes.decode('utf-8')
