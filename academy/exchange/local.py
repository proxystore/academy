# ruff: noqa: D102
from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
from typing import Any
from typing import Generic

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from aiologic import Lock
from culsans import AsyncQueue
from culsans import AsyncQueueEmpty as QueueEmpty
from culsans import AsyncQueueShutDown as QueueShutDown
from culsans import Queue

from academy.agent import Agent
from academy.agent import AgentT
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange import ExchangeFactory
from academy.exchange.transport import _respond_pending_requests_on_terminate
from academy.exchange.transport import ExchangeTransportMixin
from academy.exchange.transport import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId
from academy.message import Message
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)


class _LocalExchangeState(NoPickleMixin):
    """Local process message exchange.

    LocalExchange is a special case of an exchange where the mailboxes
    of the exchange live in process memory. This class stores the state
    of the exchange.
    """

    def __init__(self) -> None:
        self.queues: dict[EntityId, AsyncQueue[Message[Any]]] = {}
        self.locks: dict[EntityId, Lock] = {}
        self.agents: dict[AgentId[Any], type[Agent]] = {}


@dataclasses.dataclass
class LocalAgentRegistration(Generic[AgentT]):
    """Agent registration for thread exchanges."""

    agent_id: AgentId[AgentT]
    """Unique identifier for the agent created by the exchange."""


class LocalExchangeTransport(ExchangeTransportMixin, NoPickleMixin):
    """Local exchange client bound to a specific mailbox."""

    def __init__(
        self,
        mailbox_id: EntityId,
        state: _LocalExchangeState,
    ) -> None:
        self._mailbox_id = mailbox_id
        self._state = state

    @classmethod
    def new(
        cls,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        state: _LocalExchangeState,
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
            mailbox_id = UserId.new(name=name)
            state.queues[mailbox_id] = Queue().async_q
            state.locks[mailbox_id] = Lock()
            logger.info('Registered %s in exchange', mailbox_id)
        return cls(mailbox_id, state)

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    async def close(self) -> None:
        pass

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        found: list[AgentId[Any]] = []
        for aid, type_ in self._state.agents.items():
            if agent is type_ or (
                allow_subclasses and issubclass(type_, agent)
            ):
                found.append(aid)
        alive = tuple(
            aid for aid in found if not self._state.queues[aid].is_shutdown
        )
        return alive

    def factory(self) -> LocalExchangeFactory:
        return LocalExchangeFactory(_state=self._state)

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        queue = self._state.queues[self.mailbox_id]
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
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
    ) -> LocalAgentRegistration[AgentT]:
        aid: AgentId[AgentT] = AgentId.new(name=name)
        self._state.queues[aid] = Queue().async_q
        self._state.locks[aid] = Lock()
        self._state.agents[aid] = agent
        return LocalAgentRegistration(agent_id=aid)

    async def send(self, message: Message[Any]) -> None:
        queue = self._state.queues.get(message.dest, None)
        if queue is None:
            raise BadEntityIdError(message.dest)
        async with self._state.locks[message.dest]:
            try:
                await queue.put(message)
            except QueueShutDown:
                raise MailboxTerminatedError(message.dest) from None

    async def status(self, uid: EntityId) -> MailboxStatus:
        if uid not in self._state.queues:
            return MailboxStatus.MISSING
        async with self._state.locks[uid]:
            if self._state.queues[uid].is_shutdown:
                return MailboxStatus.TERMINATED
            return MailboxStatus.ACTIVE

    async def terminate(self, uid: EntityId) -> None:
        queue = self._state.queues.get(uid, None)
        if queue is None:
            return

        async with self._state.locks[uid]:
            if queue.is_shutdown:
                return

            messages = await _drain_queue(queue)
            await _respond_pending_requests_on_terminate(messages, self)

            queue.shutdown(immediate=True)
            if isinstance(uid, AgentId):
                self._state.agents.pop(uid, None)


class LocalExchangeFactory(
    ExchangeFactory[LocalExchangeTransport],
    NoPickleMixin,
):
    """Local exchange client factory.

    A thread exchange can be used to pass messages between agents running
    in separate threads of a single process.
    """

    def __init__(
        self,
        *,
        _state: _LocalExchangeState | None = None,
    ):
        self._state = _LocalExchangeState() if _state is None else _state

    async def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        registration: LocalAgentRegistration[Any] | None = None,  # type: ignore[override]
    ) -> LocalExchangeTransport:
        return LocalExchangeTransport.new(
            mailbox_id,
            name=name,
            state=self._state,
        )


async def _drain_queue(queue: AsyncQueue[Message[Any]]) -> list[Message[Any]]:
    items: list[Message[Any]] = []

    while True:
        try:
            item = queue.get_nowait()
        except (QueueShutDown, QueueEmpty):
            break
        else:
            items.append(item)
            queue.task_done()

    return items
