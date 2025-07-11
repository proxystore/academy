from __future__ import annotations

import contextlib
import enum
import sys
from collections.abc import Iterable
from types import TracebackType
from typing import Any
from typing import Protocol
from typing import runtime_checkable
from typing import TYPE_CHECKING
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.agent import Agent
from academy.agent import AgentT
from academy.exception import MailboxTerminatedError
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ErrorResponse
from academy.message import Message

if TYPE_CHECKING:
    from academy.exchange import ExchangeFactory


class MailboxStatus(enum.Enum):
    """Exchange mailbox status."""

    MISSING = 'MISSING'
    """Mailbox does not exist."""
    ACTIVE = 'ACTIVE'
    """Mailbox exists and is accepting messages."""
    TERMINATED = 'TERMINATED'
    """Mailbox was terminated and no longer accepts messages."""


@runtime_checkable
class AgentRegistration(Protocol[AgentT]):
    """Agent exchange registration information.

    Attributes:
        agent_id: Unique agent identifier returned by the exchange.
    """

    agent_id: AgentId[AgentT]


AgentRegistrationT = TypeVar(
    'AgentRegistrationT',
    bound=AgentRegistration[Any],
)
"""Type variable bound [`AgentRegistration`][academy.exchange.transport.AgentRegistration]."""  # noqa: E501
AgentRegistrationT_co = TypeVar('AgentRegistrationT_co', covariant=True)


@runtime_checkable
class ExchangeTransport(Protocol[AgentRegistrationT_co]):
    """Low-level exchange communicator.

    A message exchange hosts mailboxes for each entity (i.e., agent or
    user) in a multi-agent system. This transport protocol defines mechanisms
    for entity management (e.g., registration, discovery, status, termination)
    and for sending/receiving messages from a mailbox. As such, each transport
    instance is "bound" to a specific mailbox in the exchange.

    Warning:
        A specific exchange transport should not be replicated because multiple
        client instances receiving from the same mailbox produces undefined
        agent.
    """

    @property
    def mailbox_id(self) -> EntityId:
        """ID of the mailbox this client is bound to."""
        ...

    async def close(self) -> None:
        """Close the exchange client.

        Note:
            This does not alter the state of the mailbox this client is bound
            to. I.e., the mailbox will not be terminated.
        """
        ...

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        """Discover peer agents with a given agent.

        Warning:
            Implementations of this method are often O(n) and scan the types
            of all agents registered to the exchange.

        Args:
            agent: Agent type of interest.
            allow_subclasses: Return agents implementing subclasses of the
                agent.

        Returns:
            Tuple of agent IDs implementing the agent.

        Raises:
            ExchangeError: Error returned by the exchange.
        """
        ...

    def factory(self) -> ExchangeFactory[Self]:
        """Get an exchange factory."""
        ...

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        """Receive the next message sent to the mailbox.

        This blocks until the next message is received, there is a timeout, or
        the mailbox is terminated.

        Args:
            timeout: Optional timeout in seconds to wait for the next
                message. If `None`, the default, block forever until the
                next message or the mailbox is closed.

        Raises:
            MailboxTerminatedError: If the mailbox was closed.
            ExchangeError: Error returned by the exchange.
            TimeoutError: If a `timeout` was specified and exceeded.
        """
        ...

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> AgentRegistrationT_co:
        """Register a new agent and associated mailbox with the exchange.

        Args:
            agent: Agent type of the agent.
            name: Optional display name for the agent.

        Returns:
            Agent registration info.

        Raises:
            ExchangeError: Error returned by the exchange.
        """
        ...

    async def send(self, message: Message[Any]) -> None:
        """Send a message to a mailbox.

        Args:
            message: Message to send.

        Raises:
            BadEntityIdError: If a mailbox for `message.dest` does not exist.
            MailboxTerminatedError: If the mailbox was closed.
            ExchangeError: Error returned by the exchange.
        """
        ...

    async def status(self, uid: EntityId) -> MailboxStatus:
        """Check the status of a mailbox in the exchange.

        Args:
            uid: Entity identifier of the mailbox to check.

        Raises:
            ExchangeError: Error returned by the exchange.
        """
        ...

    async def terminate(self, uid: EntityId) -> None:
        """Terminate a mailbox in the exchange.

        Once an entity's mailbox is terminated:

        * All request messages in the mailbox will be replied to with a
          [`MailboxTerminatedError`][academy.exception.MailboxTerminatedError].
        * All calls to
          [`recv()`][academy.exchange.transport.ExchangeTransport.recv]
          will raise a
          [`MailboxTerminatedError`][academy.exception.MailboxTerminatedError].
        * All attempts to
          [`send()`][academy.exchange.transport.ExchangeTransport.send]
          to this mailbox by other entities will raise a
          [`MailboxTerminatedError`][academy.exception.MailboxTerminatedError].

        Note:
            This method is a no-op if the mailbox does not exist.

        Args:
            uid: Entity identifier of the mailbox to close.

        Raises:
            ExchangeError: Error returned by the exchange.
        """
        ...


ExchangeTransportT = TypeVar(
    'ExchangeTransportT',
    bound=ExchangeTransport[Any],
)
"""Type variable bound [`ExchangeTransport`][academy.exchange.transport.ExchangeTransport]."""  # noqa: E501


class ExchangeTransportMixin:
    """Magic method mixin for exchange transport implementations.

    Adds `__repr__`, `__str__`, and context manager support.
    """

    def __repr__(self: ExchangeTransportT) -> str:
        return f'{type(self).__name__}({self.mailbox_id!r})'

    def __str__(self: ExchangeTransportT) -> str:
        return f'{type(self).__name__}<{self.mailbox_id}>'

    async def __aenter__(self: ExchangeTransportT) -> ExchangeTransportT:
        return self

    async def __aexit__(
        self: ExchangeTransportT,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self.close()


async def _respond_pending_requests_on_terminate(
    messages: Iterable[Message[Any]],
    transport: ExchangeTransport[Any],
) -> None:
    # Helper function used to parse all pending messages in a mailbox when
    # it is terminated and reply to only request messages with a
    # MailboxTerminatedError.
    for message in messages:
        if message.is_request():
            error = MailboxTerminatedError(transport.mailbox_id)
            response = message.create_response(ErrorResponse(exception=error))
            # If the requester's mailbox was also terminated then they
            # don't need to get a response.
            with contextlib.suppress(MailboxTerminatedError):
                await transport.send(response)
