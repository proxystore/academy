from __future__ import annotations

import contextlib
import logging
import multiprocessing
import sys
import uuid
from collections.abc import Generator
from typing import Any
from typing import Callable
from typing import Literal
from typing import TypeVar

from academy.exchange import BoundExchangeClient
from academy.exchange import MailboxStatus
from academy.exchange import UnboundExchangeClient

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    pass
else:  # pragma: <3.11 cover
    pass

import requests

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange.cloud.config import ExchangeServingConfig
from academy.exchange.cloud.server import _FORBIDDEN_CODE
from academy.exchange.cloud.server import _NOT_FOUND_CODE
from academy.exchange.cloud.server import _run
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import BaseMessage
from academy.message import Message
from academy.message import PingRequest
from academy.message import RequestMessage
from academy.socket import wait_connection

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class UnboundHttpExchangeClient(UnboundExchangeClient):
    """Http exchange client.

    Args:
        host: Host name of the exchange server.
        port: Port of the exchange server.
        additional_headers: Any other information necessary to communicate
            with the exchange. Used for passing the Globus bearer token
        scheme: HTTP scheme, non-protected "http" by default.
        ssl_verify: Same as requests.Session.verify. If the server's TLS
            certificate should be validated. Should be true if using HTTPS
            Only set to false for testing or local development.
    """

    def __init__(
        self,
        host: str,
        port: int,
        additional_headers: dict[str, str] | None = None,
        scheme: Literal['http', 'https'] = 'http',
        ssl_verify: str | bool | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.additional_headers = additional_headers
        self.scheme = scheme
        self.ssl_verify = ssl_verify

    def _bind(
        self,
        mailbox_id: EntityId | None = None,
        name: str | None = None,
        handler: Callable[[RequestMessage], None] | None = None,
        *,
        start_listener: bool,
    ) -> BoundHttpExchangeClient:
        """Bind exchange to client or agent.

        If no agent is provided, exchange should create a new mailbox without
        an associated behavior and bind to that. Otherwise, the exchange will
        bind to the mailbox associated with the provided agent.

        Note:
            This is intentionally restrictive. Each user or agent should only
            bind to the exchange with a single address. This forces
            multiplexing of handles to other agents and requests to this
            agents.
        """
        return BoundHttpExchangeClient(
            self,
            mailbox_id=mailbox_id,
            name=name,
            handler=handler,
            start_listener=start_listener,
        )


class BoundHttpExchangeClient(BoundExchangeClient):
    """Http exchange client.

    Args:
        host: Host name of the exchange server.
        port: Port of the exchange server.
        additional_headers: Any other information necessary to communicate
            with the exchange. Used for passing the Globus bearer token
        scheme: HTTP scheme, non-protected "http" by default.
        ssl_verify: Same as requests.Session.verify. If the server's TLS
            certificate should be validated. Should be true if using HTTPS
            Only set to false for testing or local development.
    """

    def __init__(
        self,
        unbound: UnboundHttpExchangeClient,
        mailbox_id: EntityId | None = None,
        name: str | None = None,
        handler: Callable[[RequestMessage], None] | None = None,
        *,
        start_listener: bool,
    ) -> None:
        self.host = unbound.host
        self.port = unbound.port
        self.additional_headers = unbound.additional_headers
        self.scheme = unbound.scheme
        self.ssl_verify = unbound.ssl_verify

        self._session = requests.Session()
        if self.additional_headers is not None:
            self._session.headers.update(self.additional_headers)

        if self.ssl_verify is not None:
            self._session.verify = self.ssl_verify

        self._mailbox_url = f'{self.scheme}://{self.host}:{self.port}/mailbox'
        self._message_url = f'{self.scheme}://{self.host}:{self.port}/message'
        self._discover_url = (
            f'{self.scheme}://{self.host}:{self.port}/discover'
        )

        super().__init__(
            mailbox_id,
            name,
            handler,
            start_listener=start_listener,
        )

    def __repr__(self) -> str:
        return f'{type(self).__name__}(host="{self.host}", port={self.port})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.host}:{self.port}>'

    def __reduce__(
        self,
    ) -> tuple[
        type[UnboundHttpExchangeClient],
        tuple[
            str,
            int,
            dict[str, str] | None,
            Literal['http', 'https'],
            str | bool | None,
        ],
    ]:
        return (
            UnboundHttpExchangeClient,
            (
                self.host,
                self.port,
                self.additional_headers,
                self.scheme,
                self.ssl_verify,
            ),
        )

    def close(self) -> None:
        """Close this exchange client."""
        super().close()

        self._session.close()
        logger.debug('Closed exchange (%s)', self)

    def status(self, mailbox_id: EntityId) -> MailboxStatus:
        """Check status of mailbox on exchange."""
        # TODO: Do we need to create a new endpoint to check status?
        try:
            self.send(
                mailbox_id,
                PingRequest(
                    src=self.mailbox_id,
                    dest=mailbox_id,
                ),
            )
        except BadEntityIdError:
            return MailboxStatus.MISSING
        except MailboxClosedError:
            return MailboxStatus.TERMINATED

        return MailboxStatus.ACTIVE

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        agent_id: AgentId[BehaviorT] | None = None,
        name: str | None = None,
    ) -> AgentId[BehaviorT]:
        """Create a new agent identifier and associated mailbox.

        Args:
            behavior: Type of the behavior this agent will implement.
            agent_id: Specify the ID of the agent. Randomly generated
                default.
            name: Optional human-readable name for the agent. Ignored if
                `agent_id` is provided.

        Returns:
            Unique identifier for the agent's mailbox.
        """
        aid = AgentId.new(name=name) if agent_id is None else agent_id
        response = self._session.post(
            self._mailbox_url,
            json={
                'mailbox': aid.model_dump_json(),
                'behavior': ','.join(behavior.behavior_mro()),
            },
        )
        response.raise_for_status()
        logger.debug('Registered %s in %s', aid, self)
        return aid

    def _register_client(
        self,
        *,
        name: str | None = None,
    ) -> ClientId:
        """Create a new client identifier and associated mailbox.

        Args:
            name: Optional human-readable name for the client.

        Returns:
            Unique identifier for the client's mailbox.
        """
        cid = ClientId.new(name=name)
        response = self._session.post(
            self._mailbox_url,
            json={'mailbox': cid.model_dump_json()},
        )
        response.raise_for_status()
        logger.debug('Registered %s in %s', cid, self)
        return cid

    def terminate(self, uid: EntityId) -> None:
        """Close the mailbox for an entity from the exchange.

        Note:
            This method is a no-op if the mailbox does not exists.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        response = self._session.delete(
            self._mailbox_url,
            json={'mailbox': uid.model_dump_json()},
        )
        response.raise_for_status()
        logger.debug('Closed mailbox for %s (%s)', uid, self)

    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        """Discover peer agents with a given behavior.

        Warning:
            Discoverability is not implemented on the HTTP exchange.

        Args:
            behavior: Behavior type of interest.
            allow_subclasses: Return agents implementing subclasses of the
                behavior.

        Returns:
            Tuple of agent IDs implementing the behavior.
        """
        behavior_str = f'{behavior.__module__}.{behavior.__name__}'
        response = self._session.get(
            self._discover_url,
            json={
                'behavior': behavior_str,
                'allow_subclasses': allow_subclasses,
            },
        )
        response.raise_for_status()
        agent_ids = [
            aid
            for aid in response.json()['agent_ids'].split(',')
            if len(aid) > 0
        ]
        return tuple(AgentId(uid=uuid.UUID(aid)) for aid in agent_ids)

    def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination address of the message.
            message: Message to send.

        Raises:
            BadEntityIdError: if a mailbox for `uid` does not exist.
            MailboxClosedError: if the mailbox was closed.
        """
        response = self._session.put(
            self._message_url,
            json={'message': message.model_dump_json()},
        )
        if response.status_code == _NOT_FOUND_CODE:
            raise BadEntityIdError(uid)
        elif response.status_code == _FORBIDDEN_CODE:
            raise MailboxClosedError(uid)
        response.raise_for_status()
        logger.debug('Sent %s to %s', type(message).__name__, uid)

    def recv(self, timeout: float | None = None) -> Message:
        """Receive the next message in the mailbox.

        This blocks until the next message is received or the mailbox
        is closed.

        Args:
            timeout: Optional timeout in seconds to wait for the next
                message. If `None`, the default, block forever until the
                next message or the mailbox is closed.

        Raises:
            MailboxClosedError: if the mailbox was closed.
            TimeoutError: if a `timeout` was specified and exceeded.
        """
        try:
            response = self._session.get(
                self._message_url,
                json={'mailbox': self.mailbox_id.model_dump_json()},
                timeout=timeout,
            )
        except requests.exceptions.Timeout as e:
            raise TimeoutError(
                f'Failed to receive response in {timeout} seconds.',
            ) from e
        if response.status_code == _FORBIDDEN_CODE:
            raise MailboxClosedError(self.mailbox_id)
        response.raise_for_status()

        message = BaseMessage.model_from_json(response.json().get('message'))
        logger.debug(
            'Received %s to %s',
            type(response).__name__,
            self.mailbox_id,
        )
        return message

    def clone(self) -> UnboundHttpExchangeClient:
        """Shallow copy exchange to new, unbound version."""
        return UnboundHttpExchangeClient(
            self.host,
            self.port,
            self.additional_headers,
            self.scheme,
            self.ssl_verify,
        )


@contextlib.contextmanager
def spawn_http_exchange(
    host: str = '0.0.0.0',
    port: int = 5463,
    *,
    level: int | str = logging.WARNING,
    timeout: float | None = None,
) -> Generator[UnboundHttpExchangeClient]:
    """Context manager that spawns an HTTP exchange in a subprocess.

    This function spawns a new process (rather than forking) and wait to
    return until a connection with the exchange has been established.
    When exiting the context manager, `SIGINT` will be sent to the exchange
    process. If the process does not exit within 5 seconds, it will be
    killed.

    Warning:
        The exclusion of authentication and ssl configuration is
        intentional. This method should only be used for temporary exchanges
        in trusted environments (i.e. the login node of a cluster).

    Args:
        host: Host the exchange should listen on.
        port: Port the exchange should listen on.
        level: Logging level.
        timeout: Connection timeout when waiting for exchange to start.

    Returns:
        Exchange interface connected to the spawned exchange.
    """
    # Fork is not safe in multi-threaded context.
    multiprocessing.set_start_method('spawn')

    config = ExchangeServingConfig(host=host, port=port, log_level=level)
    exchange_process = multiprocessing.Process(
        target=_run,
        args=(config,),
    )
    exchange_process.start()

    logger.info('Starting exchange server...')
    wait_connection(host, port, timeout=timeout)
    logger.info('Started exchange server!')

    exchange = UnboundHttpExchangeClient(
        host,
        port,
    )
    try:
        yield exchange
    finally:
        logger.info('Terminating exchange server...')
        wait = 5
        exchange_process.terminate()
        exchange_process.join(timeout=wait)
        if exchange_process.exitcode is None:  # pragma: no cover
            logger.info(
                'Killing exchange server after waiting %s seconds',
                wait,
            )
            exchange_process.kill()
        else:
            logger.info('Terminated exchange server!')
        exchange_process.close()
