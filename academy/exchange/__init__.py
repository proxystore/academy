from __future__ import annotations

import abc
import asyncio
import contextlib
import enum
import logging
import sys
import uuid
from collections.abc import Coroutine
from types import TracebackType
from typing import Any
from typing import Callable
from typing import Generic
from typing import Protocol
from typing import runtime_checkable
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import TypeAlias
else:  # pragma: <3.11 cover
    from typing_extensions import TypeAlias

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.agent import Agent
from academy.agent import AgentT
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange.transport import AgentRegistration
from academy.exchange.transport import ExchangeTransportT
from academy.exchange.transport import MailboxStatus
from academy.handle import RemoteHandle
from academy.handle import UnboundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import RequestT_co

__all__ = [
    'AgentExchangeClient',
    'ExchangeClient',
    'ExchangeFactory',
    'UserExchangeClient',
]

logger = logging.getLogger(__name__)

RequestHandler: TypeAlias = Callable[
    [Message[RequestT_co]],
    Coroutine[None, None, None],
]


class ExchangeFactory(abc.ABC, Generic[ExchangeTransportT]):
    """Exchange client factory.

    An exchange factory is used to mint new exchange clients for users and
    agents, encapsulating the complexities of instantiating the underlying
    communication classes (the
    [`ExchangeTransport`][academy.exchange.transport.ExchangeTransport]).

    Warning:
        Factory implementations must be efficiently pickleable because
        factory instances are shared between user and agent processes so
        that all entities can create clients to the same exchange.
    """

    @abc.abstractmethod
    async def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        registration: AgentRegistration[Any] | None = None,
    ) -> ExchangeTransportT: ...

    async def create_agent_client(
        self,
        registration: AgentRegistration[AgentT],
        request_handler: RequestHandler[RequestT_co],
    ) -> AgentExchangeClient[AgentT, ExchangeTransportT]:
        """Create a new agent exchange client.

        An agent must be registered with the exchange before an exchange
        client can be created. For example:
        ```python
        factory = ExchangeFactory(...)
        user_client = factory.create_user_client()
        registration = user_client.register_agent(...)
        agent_client = factory.create_agent_client(registration, ...)
        ```

        Args:
            registration: Registration information returned by the exchange.
            request_handler: Agent request message handler.

        Returns:
            Agent exchange client.

        Raises:
            BadEntityIdError: If an agent with `registration.agent_id` is not
                already registered with the exchange.
        """
        agent_id: AgentId[AgentT] = registration.agent_id
        transport = await self._create_transport(
            mailbox_id=agent_id,
            registration=registration,
        )
        assert transport.mailbox_id == agent_id
        status = await transport.status(agent_id)
        if status != MailboxStatus.ACTIVE:
            await transport.close()
            raise BadEntityIdError(agent_id)
        return AgentExchangeClient(
            agent_id,
            transport,
            request_handler=request_handler,
        )

    async def create_user_client(
        self,
        *,
        name: str | None = None,
        start_listener: bool = True,
    ) -> UserExchangeClient[ExchangeTransportT]:
        """Create a new user in the exchange and associated client.

        Args:
            name: Display name of the client on the exchange.
            start_listener: Start a message listener thread.

        Returns:
            User exchange client.
        """
        transport = await self._create_transport(mailbox_id=None, name=name)
        user_id = transport.mailbox_id
        assert isinstance(user_id, UserId)
        return UserExchangeClient(
            user_id,
            transport,
            start_listener=start_listener,
        )


class ExchangeClient(abc.ABC, Generic[ExchangeTransportT]):
    """Base exchange client.

    Warning:
        Exchange clients should only be created via
        [`ExchangeFactory.create_agent_client()`][academy.exchange.ExchangeFactory.create_agent_client]
        or
        [`ExchangeFactory.create_user_client()`][academy.exchange.ExchangeFactory.create_user_client]!

    Args:
        transport: Exchange transport bound to a mailbox.
    """

    def __init__(
        self,
        transport: ExchangeTransportT,
    ) -> None:
        self._transport = transport
        self._handles: dict[uuid.UUID, RemoteHandle[Any]] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.client_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.client_id}>'

    @property
    @abc.abstractmethod
    def client_id(self) -> EntityId:
        """Client ID as registered with the exchange."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the transport."""
        ...

    async def _close_handles(self) -> None:
        """Close all handles created by this client."""
        for key in tuple(self._handles):
            handle = self._handles.pop(key)
            await handle.close(wait_futures=False)

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        """Discover peer agents with a given agent.

        Args:
            agent: Agent type of interest.
            allow_subclasses: Return agents implementing subclasses of the
                agent.

        Returns:
            Tuple of agent IDs implementing the agent.
        """
        return await self._transport.discover(
            agent,
            allow_subclasses=allow_subclasses,
        )

    def factory(self) -> ExchangeFactory[ExchangeTransportT]:
        """Get an exchange factory."""
        return self._transport.factory()

    def get_handle(self, aid: AgentId[AgentT]) -> RemoteHandle[AgentT]:
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
        handle = RemoteHandle(self, aid)
        self._handles[handle.handle_id] = handle
        logger.info('Created handle to %s', aid)
        return handle

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> AgentRegistration[AgentT]:
        """Register a new agent and associated mailbox with the exchange.

        Args:
            agent: Agent type of the agent.
            name: Optional display name for the agent.

        Returns:
            Agent registration info.
        """
        registration = await self._transport.register_agent(
            agent,
            name=name,
        )
        logger.info('Registered %s in exchange', registration.agent_id)
        return registration

    async def send(self, message: Message[Any]) -> None:
        """Send a message to a mailbox.

        Args:
            message: Message to send.

        Raises:
            BadEntityIdError: If a mailbox for `message.dest` does not exist.
            MailboxTerminatedError: If the mailbox was closed.
        """
        await self._transport.send(message)
        logger.debug('Sent %s to %s', type(message).__name__, message.dest)

    async def status(self, uid: EntityId) -> MailboxStatus:
        """Check the status of a mailbox in the exchange.

        Args:
            uid: Entity identifier of the mailbox to check.
        """
        return await self._transport.status(uid)

    async def terminate(self, uid: EntityId) -> None:
        """Terminate a mailbox in the exchange.

        Terminating a mailbox means that the corresponding entity will no
        longer be able to receive messages.

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        await self._transport.terminate(uid)

    async def _listen_for_messages(self) -> None:
        while True:
            try:
                message = await self._transport.recv()
            except (asyncio.CancelledError, MailboxTerminatedError):
                break
            logger.debug(
                'Received %s from %s for %s',
                type(message).__name__,
                message.src,
                self.client_id,
            )
            await self._handle_message(message)

    @abc.abstractmethod
    async def _handle_message(self, message: Message[Any]) -> None: ...


class AgentExchangeClient(
    ExchangeClient[ExchangeTransportT],
    Generic[AgentT, ExchangeTransportT],
):
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
        agent_id: AgentId[AgentT],
        transport: ExchangeTransportT,
        request_handler: RequestHandler[RequestT_co],
    ) -> None:
        super().__init__(transport)
        self._agent_id = agent_id
        self._request_handler = request_handler

    @property
    def client_id(self) -> AgentId[AgentT]:
        """Agent ID of the client."""
        return self._agent_id

    async def close(self) -> None:
        """Close the user client.

        This closes the underlying exchange transport and all handles created
        by this client. The agent's mailbox will not be terminated so the agent
        can be started again later.
        """
        async with self._close_lock:
            if self._closed:
                return

            await self._close_handles()
            await self._transport.close()
            self._closed = True
            logger.info('Closed exchange client for %s', self.client_id)

    async def _handle_message(self, message: Message[Any]) -> None:
        if message.is_request():
            await self._request_handler(message)
        elif message.is_response():
            if message.label is None or message.label not in self._handles:
                logger.warning(
                    'Exchange client for %s received an unexpected response '
                    'message from %s but no corresponding handle exists.',
                    self.client_id,
                    message.src,
                )
                return
            handle = self._handles[message.label]
            await handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')


class UserExchangeClient(ExchangeClient[ExchangeTransportT]):
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
        user_id: UserId,
        transport: ExchangeTransportT,
        *,
        start_listener: bool = True,
    ) -> None:
        super().__init__(transport)
        self._user_id = user_id
        self._listener_task: asyncio.Task[None] | None = None
        if start_listener:
            self._listener_task = asyncio.create_task(
                self._listen_for_messages(),
                name=f'user-exchange-listener-{self.client_id}',
            )

    @property
    def client_id(self) -> UserId:
        """User ID of the client."""
        return self._user_id

    async def close(self) -> None:
        """Close the user client.

        This terminates the user's mailbox, closes the underlying exchange
        transport, and closes all handles produced by this client.
        """
        async with self._close_lock:
            if self._closed:
                return

            await self._close_handles()
            await self._transport.terminate(self.client_id)
            logger.info(f'Terminated mailbox for {self.client_id}')
            await self._stop_listener_task()
            await self._transport.close()
            self._closed = True
            logger.info('Closed exchange client for %s', self.client_id)

    async def _handle_message(self, message: Message[Any]) -> None:
        if message.is_request():
            error = TypeError(f'{self.client_id} cannot fulfill requests.')
            response = message.create_response(ErrorResponse(exception=error))
            await self._transport.send(response)
            logger.warning(
                'Exchange client for %s received unexpected request message '
                'from %s',
                self.client_id,
                message.src,
            )
        elif message.is_response():
            if (
                message.label is None or message.label not in self._handles
            ):  # pragma: no cover
                logger.warning(
                    'Exchange client for %s received an unexpected response '
                    'message from %s but no corresponding handle exists.',
                    self.client_id,
                    message.src,
                )
                return
            handle = self._handles[message.label]
            await handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')

    async def _stop_listener_task(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
