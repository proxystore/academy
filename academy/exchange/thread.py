# ruff: noqa: D102
from __future__ import annotations

import logging
import sys
from typing import Any
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeFactory
from academy.exchange import ExchangeTransport
from academy.exchange import MailboxStatus
from academy.exchange.queue import Queue
from academy.exchange.queue import QueueClosedError
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.identifier import EntityId
from academy.message import Message
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class _ThreadExchangeState(NoPickleMixin):
    """Local process message exchange for threaded agents.

    ThreadExchange is a special case of an exchange where the mailboxes
    of the exchange live in process memory. This class stores the state
    of the exchange.
    """

    def __init__(self) -> None:
        self.queues: dict[EntityId, Queue[Message]] = {}
        self.behaviors: dict[AgentId[Any], type[Behavior]] = {}


class ThreadExchangeFactory(ExchangeFactory, NoPickleMixin):
    """Local exchange client factory.

    A thread exchange can be used to pass messages between agents running
    in separate threads of a single process.
    """

    def __init__(
        self,
        *,
        _state: _ThreadExchangeState | None = None,
    ):
        self._state = _ThreadExchangeState() if _state is None else _state

    def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
    ) -> ThreadExchangeTransport:
        return ThreadExchangeTransport.new(
            mailbox_id,
            name=name,
            state=self._state,
        )


class ThreadExchangeTransport(ExchangeTransport, NoPickleMixin):
    """Thread exchange client bound to a specific mailbox."""

    def __init__(
        self,
        mailbox_id: EntityId,
        state: _ThreadExchangeState,
    ) -> None:
        self._mailbox_id = mailbox_id
        self._state = state

    @classmethod
    def new(
        cls,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        state: _ThreadExchangeState,
    ) -> Self:
        """Instantiate a new transport.

        Args:
            mailbox_id: Bind the transport to the specific mailbox. If `None`,
                a new user entity will be registered and the transport will be
                bound to that mailbox.
            name: Display name of the redistered entity if `mailbox_id` is
                `None`.
            state: Shared state among exchange clients.

        Returns:
            An instantiated transport bound to a specific mailbox.
        """
        if mailbox_id is None:
            mailbox_id = ClientId.new(name=name)
            state.queues[mailbox_id] = Queue()
            logger.info('Registered %s in exchange', mailbox_id)
        return cls(mailbox_id, state)

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    def close(self) -> None:
        pass

    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
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

    def factory(self) -> ThreadExchangeFactory:
        return ThreadExchangeFactory(_state=self._state)

    def recv(self, timeout: float | None = None) -> Message:
        try:
            return self._state.queues[self.mailbox_id].get(timeout=timeout)
        except QueueClosedError as e:
            raise MailboxClosedError(self.mailbox_id) from e

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
        _agent_id: AgentId[BehaviorT] | None = None,
    ) -> AgentId[BehaviorT]:
        aid: AgentId[BehaviorT] = (
            AgentId.new(name=name) if _agent_id is None else _agent_id
        )
        self._state.queues[aid] = Queue()
        self._state.behaviors[aid] = behavior
        return aid

    def send(self, uid: EntityId, message: Message) -> None:
        queue = self._state.queues.get(uid, None)
        if queue is None:
            raise BadEntityIdError(uid)
        try:
            queue.put(message)
        except QueueClosedError as e:
            raise MailboxClosedError(uid) from e

    def status(self, uid: EntityId) -> MailboxStatus:
        if uid not in self._state.queues:
            return MailboxStatus.MISSING
        if self._state.queues[uid].closed():
            return MailboxStatus.TERMINATED
        return MailboxStatus.ACTIVE

    def terminate(self, uid: EntityId) -> None:
        queue = self._state.queues.get(uid, None)
        if queue is not None and not queue.closed():
            queue.close()
            if isinstance(uid, AgentId):
                self._state.behaviors.pop(uid, None)
