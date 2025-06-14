from __future__ import annotations

import logging
from typing import Any
from typing import Callable
from typing import TypeVar

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.exchange import MailboxStatus
from academy.exchange.queue import Queue
from academy.exchange.queue import QueueClosedError
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import Message
from academy.message import RequestMessage
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class ThreadExchangeState(NoPickleMixin):
    """Local process message exchange for threaded agents.

    ThreadExchange is a special case of an exchange where the mailboxes
    of the exchange live in process memory. This class stores the state
    of the exchange.
    """

    def __init__(self) -> None:
        self.queues: dict[EntityId, Queue[Message]] = {}
        self.behaviors: dict[AgentId[Any], type[Behavior]] = {}


class ThreadExchangeFactory(ExchangeFactory):
    """A unbound thread exchange.

    A thread exchange can be used to pass messages between agents
    within a process.

    Args:
        exchange_state: The state of the queues used by the exchange
    """

    def __init__(self, exchange_state: ThreadExchangeState | None = None):
        self._state = (
            ThreadExchangeState() if exchange_state is None else exchange_state
        )

    def _bind(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        handler: Callable[[RequestMessage], None] | None = None,
        start_listener: bool,
    ) -> ThreadExchangeClient:
        return ThreadExchangeClient(
            self._state,
            mailbox_id,
            name=name,
            handler=handler,
            start_listener=start_listener,
        )


class ThreadExchangeClient(ExchangeClient):
    """A thread exchange bound to a mailbox.

    A thread exchange can be used to pass messages between agents
    within a process.

    Args:
        exchange_state: The state of the queues used by the exchange
        mailbox_id: Identifier of the mailbox on the exchange. If there is
            not an id provided, the exchange will create a new client mailbox.
        name: Display name of mailbox on exchange.
        handler: Callback to handler requests to this exchange.
    """

    def __init__(
        self,
        exchange_state: ThreadExchangeState,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        handler: Callable[[RequestMessage], None] | None = None,
        start_listener: bool,
    ):
        self._state = exchange_state
        super().__init__(
            mailbox_id,
            name=name,
            handler=handler,
            start_listener=start_listener,
        )

    async def close(self) -> None:
        """Close the exchange.

        This will leave the queues in the state open.
        """
        await super().close()
        logger.debug('Closed exchange (%s)', self)

    async def status(self, mailbox_id: EntityId) -> MailboxStatus:
        """Check status of mailbox on exchange."""
        if mailbox_id not in self._state.queues:
            return MailboxStatus.MISSING
        if self._state.queues[mailbox_id].closed():
            return MailboxStatus.TERMINATED
        return MailboxStatus.ACTIVE

    async def register_agent(
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
        if aid not in self._state.queues or self._state.queues[aid].closed():
            self._state.queues[aid] = Queue()
            self._state.behaviors[aid] = behavior
            logger.debug('Registered %s in %s', aid, self)
        return aid

    async def _register_client(
        self,
        name: str | None = None,
    ) -> ClientId:
        """Create a new client identifier and associated mailbox.

        Args:
            name: Optional human-readable name for the client.

        Returns:
            Unique identifier for the client's mailbox.
        """
        cid = ClientId.new(name=name)
        self._state.queues[cid] = Queue()
        logger.debug('Registered %s in %s', cid, self)
        return cid

    async def terminate(self, uid: EntityId) -> None:
        """Close the mailbox for an entity from the exchange.

        Note:
            This method is a no-op if the mailbox does not exists.

        Args:
            uid: Entity identifier of the mailbox to close.
        """
        queue = self._state.queues.get(uid, None)
        if queue is not None and not queue.closed():
            queue.close()
            if isinstance(uid, AgentId):
                self._state.behaviors.pop(uid, None)
            logger.debug('Closed mailbox for %s (%s)', uid, self)

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
        found: list[AgentId[Any]] = []
        for aid, type_ in self._state.behaviors.items():
            if behavior is type_ or (
                allow_subclasses and issubclass(type_, behavior)
            ):
                found.append(aid)
        alive = tuple(
            aid for aid in found if not self._state.queues[aid].closed()
        )
        return alive

    async def send(self, uid: EntityId, message: Message) -> None:
        """Send a message to a mailbox.

        Args:
            uid: Destination address of the message.
            message: Message to send.

        Raises:
            BadEntityIdError: if a mailbox for `uid` does not exist.
            MailboxClosedError: if the mailbox was closed.
        """
        queue = self._state.queues.get(uid, None)
        if queue is None:
            raise BadEntityIdError(uid)
        try:
            queue.put(message)
            logger.debug('Sent %s to %s', type(message).__name__, uid)
        except QueueClosedError as e:
            raise MailboxClosedError(uid) from e

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
        try:
            queue = self._state.queues[self.mailbox_id]
            message = queue.get(timeout=timeout)
            logger.debug(
                'Received %s to %s',
                type(message).__name__,
                self.mailbox_id,
            )
            return message
        except QueueClosedError as e:
            raise MailboxClosedError(self.mailbox_id) from e

    def clone(self) -> ThreadExchangeFactory:
        """Shallow copy exchange to new, unbound version."""
        return ThreadExchangeFactory(self._state)
