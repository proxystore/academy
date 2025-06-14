from __future__ import annotations

import asyncio
import abc
import enum
import logging
import sys
import uuid
from types import TracebackType
from typing import Any
from typing import Callable
from typing import get_args
from typing import Protocol
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

__all__ = ['ExchangeClient', 'ExchangeFactory']

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class MailboxStatus(enum.Enum):
    MISSING = 'MISSING'
    ACTIVE = 'ACTIVE'
    TERMINATED = 'TERMINATED'


class ExchangeFactory(abc.ABC):
    """Message exchange client protocol.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    client) in a multi-agent system. With
    [`ExchangeClient`][academy.exchange.ExchangeClient], This
    protocol defines the client interface to an arbitrary exchange.
    An unbound exchange is used to attach to an existing mailbox or create
    a new mailbox. No messages or commands can be sent till a client is
    bound to a mailbox.

    Warning:
        ExchangeFactory implementations should be efficiently pickleable
        so that agents and remote clients can establish client connections
        to the same exchange.
    """

    @abc.abstractmethod
    def _bind(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        handler: Callable[[RequestMessage], None] | None = None,
        start_listener: bool,
    ) -> ExchangeClient:
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
        ...

    def bind_as_client(
        self,
        *,
        name: str | None = None,
        start_listener: bool = True,
    ) -> ExchangeClient:
        """Bind exchange to a new client mailbox.

        This method will create a new mailbox and enable this client to
        message other entities on the exchange.

        Args:
            name: Display name of the client on the exchange.
            start_listener: Start a listener task that receives and processes
                messages for handles.
        """
        return self._bind(
            mailbox_id=None,
            name=name,
            handler=None,
            start_listener=start_listener,
        )

    def bind_as_agent(
        self,
        agent_id: AgentId[Any],
        *,
        name: str | None = None,
        handler: Callable[[RequestMessage], Coroutine[None]] | None = None,
    ) -> ExchangeClient:
        """Bind exchange to an agent mailbox.

        This method creates a exchange client bound to an agent ID.
        The agent ID must be previously created on the exchange.

        Args:
            agent_id: ID of the mailbox to receive and send messages.
            name: Display name of the client on the exchange.
            handler: Agent callback to process messages.
        """
        return self._bind(
            mailbox_id=agent_id,
            name=name,
            handler=handler,
            start_listener=False,
        )


class ExchangeClient(abc.ABC):
    """Message exchange client protocol.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    client) in a multi-agent system. With
    [`ExchangeFactory`][academy.exchange.ExchangeFactory], This
    protocol defines the client interface to an arbitrary exchange.

    Note:
        When implementing this class super().__init__ should likely be the last
        method called in the initializer. It relies on  "self._register_client"
        or "self.status".

    Warning:
        A `ExchangeClient` should not be replicated. Multiple clients
        listening to the same mailbox will lead to undefined behavior
        depending on the implementation of the exchange. Instead, clients
        should be bound to a new mailbox to be replicated.

    Args:
        mailbox_id: Identifier of the mailbox on the exchange. If there is
            not an id provided, the exchange will create a new client mailbox.
        name: Display name of mailbox on exchange.
        handler:  Callback to handle requests to this exchange.
        start_listener: Start a listener task that receives and processes
            messages for handles.
    """

    def __init__(
        self,
        mailbox_id: EntityId | None,
        *,
        name: str | None,
        handler: Callable[[RequestMessage], None] | None,
        start_listener: bool,
    ):
        loop = asyncio.get_event_loop()
        if mailbox_id is None:
            mailbox_id = loop.run_until_complete(
                self._register_client(name=name)
            )
        else:
            status = loop.run_until_complete(self.status(mailbox_id))
            if status != MailboxStatus.ACTIVE:
                raise BadEntityIdError(mailbox_id)
        
        self.mailbox_id = mailbox_id
        self.request_handler = handler
        self.bound_handles: dict[uuid.UUID, BoundRemoteHandle[Any]] = {}
        
        if start_listener:
            self._listener_task = asyncio.create_task(
                self.listen(),
                name=f'exchange-{self.mailbox_id.uid}-listener',
            )
        else:
            self._listener_task = None

    @abc.abstractmethod
    async def status(self, mailbox_id: EntityId) -> MailboxStatus:
        """Check status of a mailbox in the exchange."""
        ...

    @abc.abstractmethod
    async def _register_client(self, *, name: str | None = None) -> ClientId:
        """Create a new client identifier and associated mailbox.

        Args:
            name: Optional human-readable name for the client.

        Returns:
            Unique identifier for the client's mailbox.
        """
        ...

    @abc.abstractmethod
    async def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        agent_id: AgentId[BehaviorT] | None = None,
        name: str | None = None,
    ) -> AgentId[BehaviorT]:
        """Create a new agent identifier and associated mailbox.

        Args:
            behavior: Behavior type of the agent.
            agent_id: Specify the ID of the agent. Randomly generated default.
            name: Optional human-readable name for the agent. Ignored if
                `agent_id` is provided.

        Returns:
            Unique identifier for the agent's mailbox.
        """
        ...

    @abc.abstractmethod
    async def terminate(self, uid: EntityId) -> None:
        """Close the mailbox for an entity from the exchange.

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        ...

    @abc.abstractmethod
    async def discover(
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
        ...

    @abc.abstractmethod
    async def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination address of the message.
            message: Message to send.

        Raises:
            BadEntityIdError: if a mailbox for `uid` does not exist.
            MailboxClosedError: if the mailbox was closed.
        """
        ...

    @abc.abstractmethod
    async def recv(self, timeout: float | None = None) -> Message:
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
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the exchange client.

        Stop listening for incoming messages. This should be called before
        concrete close actions to avoid errors on the listener thread.

        Warning:
            This does not alter the state of the mailbox in the exchange for
            agent mailboxes. I.e., the exchange will still accept new messages
            to this mailbox, but this client will no longer be listening for
            them.
        """
        if isinstance(self.mailbox_id, ClientId):
            await self.terminate(self.mailbox_id)
            logger.debug(f'Terminated client mailbox {self.mailbox_id}')

        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

    @abc.abstractmethod
    def clone(self) -> ExchangeFactory:
        """Shallow copy exchange to new, unbound version."""
        ...

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self: ExchangeClient,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f'{type(self).__name__}()'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{id(self)}>'

    def get_handle(
        self,
        aid: AgentId[BehaviorT],
    ) -> BoundRemoteHandle[BehaviorT]:
        """Create a new handle to an agent.

        A handle enables a client to invoke actions on the agent.

        Note:
            It is not possible to create a handle to a client since a handle
            is essentially a new client of a specific agent.

        Args:
            aid: EntityId of the agent to create an handle to.

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

        hdl = BoundRemoteHandle(self, aid, self.mailbox_id)
        self.bound_handles[hdl.handle_id] = hdl
        return hdl

    async def _handle_request(
        self,
        request: RequestMessage,
    ) -> None:
        if self.request_handler is None:
            response = request.error(
                TypeError(
                    f'Client with {self.mailbox_id} cannot fulfill requests.',
                ),
            )
            await self.send(response.dest, response)
        else:
            await self.request_handler(request)

    def close_bound_handles(self) -> None:
        """Close all handles bound to this mailbox."""
        for key in tuple(self.bound_handles):
            handle = self.bound_handles.pop(key)
            handle.close(wait_futures=False)

    async def _message_handler(self, message: Message) -> None:
        if isinstance(message, get_args(RequestMessage)):
            await self._handle_request(message)
        elif isinstance(message, get_args(ResponseMessage)):
            try:
                handle = self.bound_handles[message.label]
            except KeyError:
                logger.exception(
                    'Receieved a response message from %s but no handle to '
                    'that agent is bound to %s.',
                    message.src,
                    self,
                )
            else:
                handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')

    async def listen(self) -> None:
        """Listen for new messages in the mailbox and process them.

        Request messages are processed via the `request_handler`, and response
        messages are dispatched to the handle that created the corresponding
        request.

        Warning:
            This method loops forever, until the mailbox is closed. Thus this
            method is typically run inside of a thread.

        Note:
            Response messages intended for a handle that does not exist
            will be logged and discarded.
        """
        try:
            while True:
                message = await self.recv()
                await self._message_handler(message)
        except MailboxClosedError:
            pass
