from __future__ import annotations

import abc
import enum
import logging
import sys
import threading
import uuid
from types import TracebackType
from typing import Any
from typing import Callable
from typing import Generic
from typing import get_args
from typing import runtime_checkable
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.handle import BoundRemoteHandle
from academy.handle import UnboundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import Message
from academy.message import RequestMessage
from academy.message import ResponseMessage

__all__ = [
    'AgentExchangeClient',
    'ExchangeClient',
    'ExchangeFactory',
    'UserExchangeClient',
]

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class MailboxStatus(enum.Enum):
    """Exchange mailbox status."""

    MISSING = 'MISSING'
    """Mailbox does not exist."""
    ACTIVE = 'ACTIVE'
    """Mailbox exists and is accepting messages."""
    TERMINATED = 'TERMINATED'
    """Mailbox was terminated and no longer accepts messages."""


class ExchangeFactory(abc.ABC):
    """Exchange client factory.

    An exchange factory is used to mint new exchange clients for users and
    agents, encapsulating the complexities of instantiating the underlying
    communication classes (the
    [`ExchangeTransport`][academy.exchange.ExchangeTransport]).

    Warning:
        Factory implementations must be efficiently pickleable because
        factory instances are shared between user and agent processes so
        that all entities can create clients to the same exchange.
    """

    @abc.abstractmethod
    def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
    ) -> ExchangeTransport: ...

    def create_agent_client(
        self,
        agent_id: AgentId[BehaviorT],
        request_handler: Callable[[RequestMessage], None],
    ) -> AgentExchangeClient[BehaviorT]:
        """Create a new agent exchange client.

        An agent must be registered with the exchange before an exchange
        client can be created. For example:
        ```python
        factory = ExchangeFactory(...)
        user_client = factory.create_user_client()
        agent_id = user_client.register_agent(...)
        agent_client = factory.create_agent_client(agent_id, ...)
        ```

        Args:
            agent_id: ID of the agent to create a client for.
            request_handler: Agent request message handler.

        Raises:
            BadEntityIdError: If an agent with `agent_id` is not already
                registered with the exchange.
        """
        transport = self._create_transport(mailbox_id=agent_id)
        assert transport.mailbox_id == agent_id
        if transport.status(agent_id) != MailboxStatus.ACTIVE:
            transport.close()
            raise BadEntityIdError(agent_id)
        return AgentExchangeClient(
            agent_id,
            transport,
            request_handler=request_handler,
        )

    def create_user_client(
        self,
        *,
        name: str | None = None,
        start_listener: bool = True,
    ) -> UserExchangeClient:
        """Create a new user in the exchange and associated client.

        Args:
            name: Display name of the client on the exchange.
            start_listener: Start a message listener thread.
        """
        transport = self._create_transport(mailbox_id=None, name=name)
        user_id = transport.mailbox_id
        assert isinstance(user_id, ClientId)
        return UserExchangeClient(
            user_id,
            transport,
            start_listener=start_listener,
        )


class ExchangeTransport(abc.ABC):
    """Low-level exchange communicator.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    user) in a multi-agent system. This transport protocol defines mechanisms
    for entity management (e.g., registration, discovery, status, termination)
    and for sending/receiving messages from a mailbox. As such, each transport
    instance is "bound" to a specific mailbox in the exchange.

    Warning:
        A specific exchange transport should not be replicated because multiple
        client instances receiving from the same mailbox produces undefined
        behavior.
    """

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.mailbox_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.mailbox_id}>'

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    @abc.abstractmethod
    def mailbox_id(self) -> EntityId:
        """ID of the mailbox this client is bound to."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        """Close the exchange client.

        Note:
            This does not alter the state of the mailbox this client is bound
            to. I.e., the mailbox will not be terminated.
        """
        ...

    @abc.abstractmethod
    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        """Discover peer agents with a given behavior.

        Warning:
            Implementations of this method are often O(n) and scan the types
            of all agents registered to the exchange.

        Args:
            behavior: Behavior type of interest.
            allow_subclasses: Return agents implementing subclasses of the
                behavior.

        Returns:
            Tuple of agent IDs implementing the behavior.
        """
        ...

    @abc.abstractmethod
    def factory(self) -> ExchangeFactory:
        """Get an exchange factory."""
        ...

    @abc.abstractmethod
    def recv(self, timeout: float | None = None) -> Message:
        """Receive the next message sent to the mailbox.

        This blocks until the next message is received, there is a timeout, or
        the mailbox is terminated.

        Args:
            timeout: Optional timeout in seconds to wait for the next
                message. If `None`, the default, block forever until the
                next message or the mailbox is closed.

        Raises:
            MailboxClosedError: If the mailbox was closed.
            TimeoutError: If a `timeout` was specified and exceeded.
        """
        ...

    @abc.abstractmethod
    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
        # This is needed by a strange hack in academy/agent.py where we
        # close an agent mailbox and immediately re-register it. This will no
        # longer be needed after Issue #100 and can be removed.
        _agent_id: AgentId[BehaviorT] | None = None,
    ) -> AgentId[BehaviorT]:
        """Register a new agent and associated mailbox with the exchange.

        Args:
            behavior: Behavior type of the agent.
            name: Optional display name for the agent.

        Returns:
            Agent ID.
        """
        ...

    @abc.abstractmethod
    def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination mailbox address for the message.
            message: Message to send.

        Raises:
            BadEntityIdError: If a mailbox for `uid` does not exist.
            MailboxClosedError: If the mailbox was closed.
        """
        ...

    @abc.abstractmethod
    def status(self, uid: EntityId) -> MailboxStatus:
        """Check the status of a mailbox in the exchange.

        Args:
            uid: Entity identifier of the mailbox to check.
        """
        ...

    @abc.abstractmethod
    def terminate(self, uid: EntityId) -> None:
        """Terminate a mailbox in the exchange.

        Terminating a mailbox means that the corresponding entity will no
        longer be able to receive messages.

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        ...


class ExchangeClient(abc.ABC):
    """Base exchange client.

    Warning:
        Exchange clients should only be created via
        [`ExchangeFactory.create_agent_client()`][academy.exchange.ExchangeFactory.create_agent_client]
        or
        [`ExchangeFactory.create_user_client()`][academy.exchange.ExchangeFactory.create_user_client]!


    Args:
        transport: Exchange transport bound to a mailbox.
    """

    def __init__(self, transport: ExchangeTransport) -> None:
        self._transport = transport
        self._handles: dict[uuid.UUID, BoundRemoteHandle[Any]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.close()

    @abc.abstractmethod
    def close(self) -> None:
        """Close the transport."""
        ...

    def _close_handles(self) -> None:
        """Close all handles created by this client."""
        for key in tuple(self._handles):
            handle = self._handles.pop(key)
            handle.close(wait_futures=False)

    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        """Discover peer agents with a given behavior.

        Args:
            behavior: Behavior type of interest.
            allow_subclasses: Return agents implementing subclasses of the
                behavior.

        Returns:
            Tuple of agent IDs implementing the behavior.
        """
        return self._transport.discover(
            behavior,
            allow_subclasses=allow_subclasses,
        )

    def factory(self) -> ExchangeFactory:
        """Get an exchange factory."""
        return self._transport.factory()

    def get_handle(
        self,
        aid: AgentId[BehaviorT],
    ) -> BoundRemoteHandle[BehaviorT]:
        """Create a new handle to an agent.

        A handle acts like a reference to a remote agent, enabling a user
        to manage the agent or asynchronously invoke actions.

        Args:
            aid: Agent to create an handle to. The agent must be registered
                with the same exchange.

        Returns:
            Handle to the agent.

        Raises:
            TypeError: if `aid` is not an instance of
                [`AgentId`][academy.identifier.AgentId].
        """
        if not isinstance(aid, AgentId):
            raise TypeError(
                f'Handle must be created from an {AgentId.__name__} '
                f'but got identifier with type {type(aid).__name__}.',
            )
        handle = BoundRemoteHandle(self, aid, self._transport.mailbox_id)
        self._handles[handle.handle_id] = handle
        logger.info('Created handle to %s', aid)
        return handle

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
        _agent_id: AgentId[BehaviorT] | None = None,
    ) -> AgentId[BehaviorT]:
        """Register a new agent and associated mailbox with the exchange.

        Args:
            behavior: Behavior type of the agent.
            name: Optional display name for the agent.

        Returns:
            Agent ID.
        """
        agent_id = self._transport.register_agent(
            behavior,
            name=name,
            _agent_id=_agent_id,
        )
        logger.info('Registered %s in exchange', agent_id)
        return agent_id

    def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination mailbox address for the message.
            message: Message to send.

        Raises:
            BadEntityIdError: If a mailbox for `uid` does not exist.
            MailboxClosedError: If the mailbox was closed.
        """
        self._transport.send(uid, message)
        logger.debug('Sent %s to %s', type(message).__name__, uid)

    def status(self, uid: EntityId) -> MailboxStatus:
        """Check the status of a mailbox in the exchange.

        Args:
            uid: Entity identifier of the mailbox to check.
        """
        return self._transport.status(uid)

    def terminate(self, uid: EntityId) -> None:
        """Terminate a mailbox in the exchange.

        Terminating a mailbox means that the corresponding entity will no
        longer be able to receive messages.

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        self._transport.terminate(uid)
        logger.debug('Terminated mailbox for %s', uid)

    def _listen_for_messages(self) -> None:
        while True:
            try:
                message = self._transport.recv()
            except MailboxClosedError:
                break
            logger.debug(
                'Received %s from %s for %s',
                type(message).__name__,
                message.src,
                self._transport.mailbox_id,
            )
            self._handle_message(message)

    @abc.abstractmethod
    def _handle_message(self, message: Message) -> None: ...


class AgentExchangeClient(ExchangeClient, Generic[BehaviorT]):
    """Agent exchange client.

    Warning:
        Agent exchange clients should only be created via
        [`ExchangeFactory.create_agent_client()`][academy.exchange.ExchangeFactory.create_agent_client]!

    Args:
        agent_id: Agent ID.
        transport: Exchange transport bound to `agent_id`.
        request_handler: Request handler of the agent that will be called
            for each message received to this agent's mailbox.
            start_listener: Start a message listener thread.
    """

    def __init__(
        self,
        agent_id: AgentId[BehaviorT],
        transport: ExchangeTransport,
        request_handler: Callable[[RequestMessage], None],
    ) -> None:
        super().__init__(transport)
        self._agent_id = agent_id
        self._request_handler = request_handler

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.agent_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.agent_id}>'

    @property
    def agent_id(self) -> AgentId[BehaviorT]:
        """Agent ID."""
        return self._agent_id

    def close(self) -> None:
        """Close the user client.

        This closes the underlying exchange transport and all handles created
        by this client. The agent's mailbox will not be terminated so the agent
        can be started again later.
        """
        self._close_handles()
        self._transport.close()
        logger.info('Closed exchange client for %s', self.agent_id)

    def _handle_message(self, message: Message) -> None:
        if isinstance(message, get_args(RequestMessage)):
            self._request_handler(message)
        elif isinstance(message, get_args(ResponseMessage)):
            try:
                handle = self._handles[message.label]
            except KeyError:
                logger.warning(
                    'Exchange client for %s received an unexpected response '
                    'message from %s but no corresponding handle exists.',
                    self.agent_id,
                    message.src,
                )
            else:
                handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')


class UserExchangeClient(ExchangeClient):
    """User exchange client.

    Warning:
        User exchange clients should only be created via
        [`ExchangeFactory.create_user_client()`][academy.exchange.ExchangeFactory.create_user_client]!

    Args:
        user_id: User ID.
        transport: Exchange transport bound to `user_id`.
        start_listener: Start a message listener thread.
    """

    def __init__(
        self,
        user_id: ClientId,
        transport: ExchangeTransport,
        *,
        start_listener: bool = True,
    ) -> None:
        super().__init__(transport)
        self._user_id = user_id
        self._listener_thread: threading.Thread | None = None
        if start_listener:
            self._listener_thread = threading.Thread(
                target=self._listen_for_messages,
                name=f'user-exchange-listener-{self.user_id.uid}',
            )
            self._listener_thread.start()

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.user_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.user_id}>'

    @property
    def user_id(self) -> ClientId:
        """User ID."""
        return self._user_id

    def close(self) -> None:
        """Close the user client.

        This terminates the user's mailbox, closes the underlying exchange
        transport, and closes all handles produced by this client.
        """
        self._close_handles()
        self._transport.terminate(self.user_id)
        logger.info(f'Terminated mailbox for {self.user_id}')
        if self._listener_thread is not None:
            self._listener_thread.join()
        self._transport.close()
        logger.info('Closed exchange client for %s', self.user_id)

    def _handle_message(self, message: Message) -> None:
        if isinstance(message, get_args(RequestMessage)):
            error = TypeError(f'{self.user_id} cannot fulfill requests.')
            response = message.error(error)
            self._transport.send(response.dest, response)
            logger.warning(
                'Exchange client for %s received unexpected request message '
                'from %s',
                self.user_id,
                message.src,
            )
        elif isinstance(message, get_args(ResponseMessage)):
            try:
                handle = self._handles[message.label]
            except KeyError:  # pragma: no cover
                logger.warning(
                    'Exchange client for %s received an unexpected response '
                    'message from %s but no corresponding handle exists.',
                    self.user_id,
                    message.src,
                )
            else:
                handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')
