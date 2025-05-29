from __future__ import annotations

import sys
from types import TracebackType
from typing import Any, Callable, get_args
from typing import Protocol
from typing import runtime_checkable
from typing import TypeVar
import uuid

from academy.agent import Agent
from academy.exception import MailboxClosedError

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.behavior import Behavior
from academy.handle import BoundRemoteHandle, ClientRemoteHandle, UnboundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import Message, RequestMessage, ResponseMessage

__all__ = ['UnboundExchangeClient', 'BoundExchangeClient', 'ExchangeMixin']

BehaviorT = TypeVar('BehaviorT', bound=Behavior)

@runtime_checkable
class UnboundExchangeClient(Protocol):
    """Message exchange client protocol.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    client) in a multi-agent system. With 
    [`BoundExchangeClient`][academy.exchange.BoundExchangeClient], This
    protocol defines the client interface to an arbitrary exchange.
    An unbound exchange is used to attach to an existing mailbox or create
    a new mailbox. No messages or commands can be sent till a client is
    bound to a mailbox.

    Warning:
        Unbound exchange implementations should be efficiently pickleable
        so that agents and remote clients can establish client connections
        to the same exchange.
    """
    def bind(self, 
             agent: Agent[BehaviorT] | None
        ) -> BoundExchangeClient:
        ...
        """Bind exchange to client or agent.

        If no agent is provided, exchange should create a new mailbox without
        an associated behavior and bind to that. Otherwise, the exchange will 
        bind to the mailbox associated with the provided agent.

        Note:
            This is intentionally restrictive. Each user or agent should only
            bind to the exchange with a single address. This forces multiplexing
            of handles to other agents and requests to this agents.
        """

@runtime_checkable
class BoundExchangeClient(Protocol):
    """Message exchange client protocol.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    client) in a multi-agent system. With 
    [`UnboundExchangeClient`][academy.exchange.UnboundExchangeClient], This
    protocol defines the client interface to an arbitrary exchange.

    Warning:
        BoundExchangeClient should not be replicated. Multiple clients
        listening to the same mailbox will lead to undefined behavior
        depending on the implementation of the exchange. Instead, clients
        should be bound to a new mailbox to be replicated.
    """

    @property
    def mailbox_id(self) -> EntityId:
        """Mailbox address/identifier."""
        ...
    
    @property
    def bound_handles(self) -> dict[uuid.UUID, BoundRemoteHandle[Any]]:
        """All handles bound to this exchange"""
        ...

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        agent_id: AgentId[BehaviorT] | None = None,
        name: str | None = None,
    ) -> AgentId[BehaviorT]:
        """Create a new agent identifier and associated mailbox.

        Args:
            behavior: Behavior type of the agent.
            agent_id: Specify the ID of the agent. Randomly generated
                default.
            name: Optional human-readable name for the agent. Ignored if
                `agent_id` is provided.

        Returns:
            Unique identifier for the agent's mailbox.
        """
        ...

    def terminate(self, uid: EntityId) -> None:
        """Close the mailbox for an entity from the exchange.

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        ...

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
        ...

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
            aid: EntityId of the agent to create a handle to.

        Returns:
            Handle to the agent.

        Raises:
            BadEntityIdError: if a mailbox for `aid` does not exist.
            TypeError: if `aid` is not an instance of
                [`AgentId`][academy.identifier.AgentId].
        """
        ...

    def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination address of the message.
            message: Message to send.

        Raises:
            BadEntityIdError: if a mailbox for `uid` does not exist.
            MailboxClosedError: if the mailbox was closed.
        """
        ...

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
        ...

    def close(self) -> None:
        """Close the exchange client.

        Stop listening for incoming messages.

        Warning:
            This does not close the mailbox in the exchange. I.e., the exchange
            will still accept new messages to this mailbox, but this client
            will no longer be listening for them.
            This does not alter the state of the exchange.
        """
        ...

    def clone(self) -> UnboundExchangeClient:
        ...

class ExchangeMixin:
    """Mixin class that adds basic methods to an exchange implementation.

    This adds a simple `repr`/`str`, context manager support, and the
    `get_handle` method.
    """

    def __enter__(self: BoundExchangeClient) -> Self:
        return self

    def __exit__(
        self: BoundExchangeClient,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f'{type(self).__name__}()'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{id(self)}>'

    def get_handle(
        self: BoundExchangeClient,
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
    
    def _handle_request(self, request: RequestMessage) -> None:
        response = request.error(
            TypeError(
                f'Client with {self.mailbox_id} cannot fulfill requests.',
            ),
        )
        self.exchange.send(response.dest, response)
    
    def _message_handler(self, message: Message) -> None:
        if isinstance(message, get_args(RequestMessage)):
            self._handle_request(message)
        elif isinstance(message, get_args(ResponseMessage)):
            try:
                handle = self.bound_handles[message.label]
            except KeyError:
                pass
            else:
                handle._process_response(message)
        else:
            raise AssertionError('Unreachable.')
        
    def listen(self: BoundExchangeClient) -> None:
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
                message = self.recv()
                self._message_handler(message)
        except MailboxClosedError:
            pass