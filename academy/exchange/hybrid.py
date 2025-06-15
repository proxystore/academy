# ruff: noqa: D102
from __future__ import annotations

import base64
import logging
import sys
import threading
import uuid
from collections.abc import Iterable
from typing import Any
from typing import get_args
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

import redis

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeFactory
from academy.exchange import ExchangeTransport
from academy.exchange import MailboxStatus
from academy.exchange.queue import Queue
from academy.exchange.queue import QueueClosedError
from academy.exchange.redis import _MailboxState
from academy.exchange.redis import _RedisConnectionInfo
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import BaseMessage
from academy.message import Message
from academy.serialize import NoPickleMixin
from academy.socket import address_by_hostname
from academy.socket import address_by_interface
from academy.socket import SimpleSocket
from academy.socket import SimpleSocketServer
from academy.socket import SocketClosedError

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)

_CLOSE_SENTINEL = b'<CLOSED>'
_THREAD_START_TIMEOUT = 5
_THREAD_JOIN_TIMEOUT = 5
_SERVER_ACK = b'<ACK>'
_SOCKET_POLL_TIMEOUT_MS = 50


class HybridExchangeFactory(ExchangeFactory):
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

    def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
    ) -> HybridExchangeTransport:
        return HybridExchangeTransport.new(
            interface=self._interface,
            mailbox_id=mailbox_id,
            name=name,
            namespace=self._namespace,
            port=None if self._ports is None else next(self._ports),
            redis_info=self._redis_info,
        )


class HybridExchangeTransport(ExchangeTransport, NoPickleMixin):
    """Hybrid exchange transport bound to a specific mailbox."""

    def __init__(  # noqa: PLR0913
        self,
        mailbox_id: EntityId,
        redis_client: redis.Redis,
        *,
        redis_info: _RedisConnectionInfo,
        namespace: str,
        interface: str | None = None,
        port: int | None = None,
    ) -> None:
        self._mailbox_id = mailbox_id
        self._redis_client = redis_client
        self._redis_info = redis_info
        self._namespace = namespace
        self._interface = interface
        # How can we pass the port through the bind method?
        self._port = port

        self._address_cache: dict[EntityId, str] = {}
        self._messages: Queue[Message] = Queue()
        self._socket_pool = _SocketPool()
        self._start_mailbox_server()

    def _address_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:address:{uuid_to_base32(uid.uid)}'

    def _behavior_key(self, aid: AgentId[Any]) -> str:
        return f'{self._namespace}:behavior:{uuid_to_base32(aid.uid)}'

    def _status_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:status:{uuid_to_base32(uid.uid)}'

    def _queue_key(self, uid: EntityId) -> str:
        return f'{self._namespace}:queue:{uuid_to_base32(uid.uid)}'

    @classmethod
    def new(  # noqa: PLR0913
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
            name: Display name of the redistered entity if `mailbox_id` is
                `None`.
            port: Port to listen for peer connection on.

        Returns:
            An instantiated transport bound to a specific mailbox.

        Raises:
            redis.exceptions.ConnectionError: If the Redis server is not
                reachable.
        """
        client = redis.Redis(
            host=redis_info.hostname,
            port=redis_info.port,
            decode_responses=False,
            **redis_info.kwargs,
        )
        # Ensure the redis server is reachable else fail early
        client.ping()

        if mailbox_id is None:
            mailbox_id = ClientId.new(name=name)
            client.set(
                f'{namespace}:status:{uuid_to_base32(mailbox_id.uid)}',
                _MailboxState.ACTIVE.value,
            )
            logger.info('Registered %s in exchange', mailbox_id)
        return cls(
            mailbox_id,
            client,
            redis_info=redis_info,
            namespace=namespace,
            interface=interface,
            port=port,
        )

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    def close(self) -> None:
        # This is necessary to get the listener thread to exit.
        # This should be replaced in the async implementation
        self._messages.close()
        self._close_mailbox_server()
        self._redis_client.close()
        self._socket_pool.close()

    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        found: list[AgentId[Any]] = []
        fqp = f'{behavior.__module__}.{behavior.__name__}'
        for key in self._redis_client.scan_iter(
            f'{self._namespace}:behavior:*',
        ):
            mro_str = self._redis_client.get(key)
            assert isinstance(mro_str, str)
            mro = mro_str.split(',')
            if fqp == mro[0] or (allow_subclasses and fqp in mro):
                aid: AgentId[Any] = AgentId(
                    uid=base32_to_uuid(key.split(':')[-1]),
                )
                found.append(aid)
        active: list[AgentId[Any]] = []
        for aid in found:
            status = self._redis_client.get(self._status_key(aid))
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

    def recv(self, timeout: float | None = None) -> Message:
        try:
            return self._messages.get(timeout=timeout)
        except QueueClosedError:
            raise MailboxClosedError(self.mailbox_id) from None

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
        _agent_id: AgentId[BehaviorT] | None = None,
    ) -> AgentId[BehaviorT]:
        aid: AgentId[Any] = (
            AgentId.new(name=name) if _agent_id is None else _agent_id
        )
        self._redis_client.set(
            self._status_key(aid),
            _MailboxState.ACTIVE.value,
        )
        self._redis_client.set(
            self._behavior_key(aid),
            ','.join(behavior.behavior_mro()),
        )
        return aid

    def _send_direct(self, address: str, message: Message) -> None:
        self._socket_pool.send(address, message.model_serialize())
        logger.debug(
            'Sent %s to %s via p2p at %s',
            type(message).__name__,
            message.dest,
            address,
        )

    def send(self, uid: EntityId, message: Message) -> None:
        address = self._address_cache.get(uid, None)
        if address is not None:
            try:
                # This is as optimistic as possible. If the address of the
                # peer is cached, we assume the mailbox is still active and
                # the peer is still listening.
                self._send_direct(address, message)
            except (SocketClosedError, OSError):
                # Our optimism let us down so clear the cache and try the
                # standard flow.
                self._address_cache.pop(uid)
            else:
                return

        status = self._redis_client.get(self._status_key(uid))
        if status is None:
            raise BadEntityIdError(uid)
        elif status == _MailboxState.INACTIVE.value:
            raise MailboxClosedError(uid)

        maybe_address = self._redis_client.get(self._address_key(uid))
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
                self._send_direct(decoded_address, message)
                self._address_cache[uid] = decoded_address
            else:
                raise TypeError('Did not active peer address in Redis.')
        except (TypeError, SocketClosedError, OSError):
            self._redis_client.rpush(
                self._queue_key(uid),
                message.model_serialize(),
            )
            logger.debug(
                'Sent %s to %s via redis',
                type(message).__name__,
                uid,
            )

    def status(self, uid: EntityId) -> MailboxStatus:
        status = self._redis_client.get(self._status_key(uid))
        if status is None:
            return MailboxStatus.MISSING
        elif status == _MailboxState.INACTIVE.value:
            return MailboxStatus.TERMINATED
        else:
            return MailboxStatus.ACTIVE

    def terminate(self, uid: EntityId) -> None:
        self._redis_client.set(
            self._status_key(uid),
            _MailboxState.INACTIVE.value,
        )
        # Sending a close sentinel to the queue is a quick way to force
        # the entity waiting on messages to the mailbox to stop blocking.
        # This assumes that only one entity is reading from the mailbox.
        self._redis_client.rpush(self._queue_key(uid), _CLOSE_SENTINEL)
        if isinstance(uid, AgentId):
            self._redis_client.delete(self._behavior_key(uid))

    def _start_mailbox_server(self) -> None:
        self._closed = threading.Event()
        self._socket_poll_timeout_ms = _SOCKET_POLL_TIMEOUT_MS

        host = (
            address_by_interface(self._interface)
            if self._interface is not None
            else address_by_hostname()
        )
        self._server = SimpleSocketServer(
            handler=self._server_handler,
            host=host,
            port=self._port,
            timeout=_THREAD_JOIN_TIMEOUT,
        )
        self._server.start_server_thread()

        self._redis_client.set(
            self._address_key(self.mailbox_id),
            f'{self._server.host}:{self._server.port}',
        )

        self._redis_thread = threading.Thread(
            target=self._redis_watcher,
            name=f'hybrid-mailbox-redis-watcher-{self.mailbox_id}',
        )
        self._redis_started = threading.Event()
        self._redis_thread.start()
        self._redis_started.wait(timeout=_THREAD_START_TIMEOUT)

    def _pull_messages_from_redis(self, timeout: int = 1) -> None:
        # Note: we use blpop instead of lpop here for the timeout.
        raw = self._redis_client.blpop(
            [self._queue_key(self.mailbox_id)],
            timeout=timeout,
        )
        if raw is None:
            return

        # Only passed one key to blpop to result is [key, item]
        assert isinstance(raw, (tuple, list))
        assert len(raw) == 2  # noqa: PLR2004
        if raw[1] == _CLOSE_SENTINEL:  # pragma: no cover
            self._messages.close()
            raise MailboxClosedError(self.mailbox_id)
        message = BaseMessage.model_deserialize(raw[1])
        assert isinstance(message, get_args(Message))
        logger.debug(
            'Received %s to %s via redis',
            type(message).__name__,
            self.mailbox_id,
        )
        self._messages.put(message)

    def _redis_watcher(self) -> None:
        self._redis_started.set()
        logger.debug('Started redis watcher thread for %s', self.mailbox_id)
        try:
            while not self._closed.is_set():
                status = self._redis_client.get(
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
                    self._messages.close()
                    break

                self._pull_messages_from_redis(timeout=1)
        except MailboxClosedError:  # pragma: no cover
            pass
        except Exception:
            logger.exception(
                'Error in redis watcher thread for %s',
                self.mailbox_id,
            )
        finally:
            self._server.stop_server_thread()
            logger.debug(
                'Stopped redis watcher thread for %s',
                self.mailbox_id,
            )

    def _server_handler(self, payload: bytes) -> bytes | None:
        message = BaseMessage.model_deserialize(payload)
        logger.debug(
            'Received %s to %s via p2p',
            type(message).__name__,
            self.mailbox_id,
        )
        self._messages.put(message)
        return None

    def _close_mailbox_server(self) -> None:
        """Close this mailbox client.

        Warning:
            This does not close the mailbox in the exchange. I.e., the exchange
            will still accept new messages to this mailbox, but this client
            will no longer be listening for them.
        """
        self._closed.set()
        self._redis_client.delete(
            self._address_key(self.mailbox_id),
        )

        self._server.stop_server_thread()

        self._redis_thread.join(_THREAD_JOIN_TIMEOUT)
        if self._redis_thread.is_alive():  # pragma: no cover
            raise TimeoutError(
                'Redis watcher thread failed to exit within '
                f'{_THREAD_JOIN_TIMEOUT} seconds.',
            )


class _SocketPool:
    def __init__(self) -> None:
        self._sockets: dict[str, SimpleSocket] = {}

    def close(self) -> None:
        for address in tuple(self._sockets.keys()):
            self.close_socket(address)

    def close_socket(self, address: str) -> None:
        conn = self._sockets.pop(address, None)
        if conn is not None:  # pragma: no branch
            conn.close(shutdown=True)

    def get_socket(self, address: str) -> SimpleSocket:
        try:
            return self._sockets[address]
        except KeyError:
            parts = address.split(':')
            host, port = parts[0], int(parts[1])
            conn = SimpleSocket(host, port)
            self._sockets[address] = conn
            return conn

    def send(self, address: str, message: bytes) -> None:
        conn = self.get_socket(address)
        try:
            conn.send(message)
        except (SocketClosedError, OSError):
            self.close_socket(address)
            raise


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
