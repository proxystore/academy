from __future__ import annotations

import sys
import threading
from concurrent.futures import Future
from typing import Any

import pytest

from academy.agent import Agent
from academy.agent import AgentRunConfig
from academy.behavior import action
from academy.behavior import Behavior
from academy.behavior import loop
from academy.exchange import UserExchangeClient
from academy.exchange.thread import ThreadExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.handle import Handle
from academy.handle import UnboundRemoteHandle
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import PingRequest
from academy.message import PingResponse
from academy.message import ShutdownRequest
from testing.behavior import CounterBehavior
from testing.behavior import EmptyBehavior
from testing.behavior import ErrorBehavior
from testing.constant import TEST_THREAD_JOIN_TIMEOUT


class SignalingBehavior(Behavior):
    def __init__(self) -> None:
        self.setup_event = threading.Event()
        self.loop_event = threading.Event()
        self.shutdown_event = threading.Event()

    def on_setup(self) -> None:
        self.setup_event.set()

    def on_shutdown(self) -> None:
        self.shutdown_event.set()

    @loop
    def waiter(self, shutdown: threading.Event) -> None:
        self.loop_event.wait()
        shutdown.wait()

    @loop
    def setter(self, shutdown: threading.Event) -> None:
        self.loop_event.set()
        shutdown.wait()


def test_agent_start_shutdown(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(SignalingBehavior)
    agent = Agent(
        SignalingBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )

    agent.start()
    agent.start()  # Idempotency check.
    agent.shutdown()
    agent.shutdown()  # Idempotency check.

    with pytest.raises(RuntimeError, match='Agent has already been shutdown.'):
        agent.start()

    assert agent.behavior.setup_event.is_set()
    assert agent.behavior.shutdown_event.is_set()


def test_agent_shutdown_without_terminate(
    exchange: UserExchangeClient[Any],
) -> None:
    registration = exchange.register_agent(SignalingBehavior)
    agent = Agent(
        SignalingBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
        config=AgentRunConfig(
            close_exchange_on_exit=False,
            terminate_on_exit=False,
        ),
    )
    agent.start()
    agent._expected_shutdown = True
    agent.shutdown()
    # Verify mailbox is open
    exchange.send(PingRequest(src=agent.agent_id, dest=agent.agent_id))


def test_agent_shutdown_without_start(
    exchange: UserExchangeClient[Any],
) -> None:
    registration = exchange.register_agent(SignalingBehavior)
    agent = Agent(
        SignalingBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )

    agent.shutdown()

    assert not agent.behavior.setup_event.is_set()
    assert agent.behavior.shutdown_event.is_set()


class LoopFailureBehavior(Behavior):
    @loop
    def bad1(self, shutdown: threading.Event) -> None:
        raise RuntimeError('Loop failure 1.')

    @loop
    def bad2(self, shutdown: threading.Event) -> None:
        raise RuntimeError('Loop failure 2.')


def test_loop_failure_triggers_shutdown(
    exchange: UserExchangeClient[Any],
) -> None:
    registration = exchange.register_agent(LoopFailureBehavior)
    agent = Agent(
        LoopFailureBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )

    agent.start()
    assert agent._shutdown.is_set()

    if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
        # In Python 3.11 and later, all exceptions are raised in a group.
        with pytest.raises(ExceptionGroup) as exc_info:  # noqa: F821
            agent.shutdown()
        assert len(exc_info.value.exceptions) == 2  # noqa: PLR2004
    else:  # pragma: <3.11 cover
        # In Python 3.10 and older, only the first error will be raised.
        with pytest.raises(RuntimeError, match='Caught at least one failure'):
            agent.shutdown()


def test_agent_run_in_thread(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(SignalingBehavior)
    agent = Agent(
        SignalingBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    assert isinstance(repr(agent), str)
    assert isinstance(str(agent), str)

    thread = threading.Thread(target=agent)
    thread.start()
    agent.behavior.setup_event.wait()
    agent.behavior.loop_event.wait()
    agent.signal_shutdown()
    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)

    assert agent.behavior.setup_event.is_set()
    assert agent.behavior.shutdown_event.is_set()


def test_agent_shutdown_message(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(EmptyBehavior)
    user_id = exchange.user_id

    agent = Agent(
        EmptyBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    thread = threading.Thread(target=agent)
    thread.start()

    shutdown = ShutdownRequest(src=user_id, dest=agent.agent_id)
    exchange.send(shutdown)

    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
    assert not thread.is_alive()


def test_agent_ping_message(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(EmptyBehavior)
    user_id = exchange.user_id

    agent = Agent(
        EmptyBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    assert isinstance(repr(agent), str)
    assert isinstance(str(agent), str)

    thread = threading.Thread(target=agent)
    thread.start()

    ping = PingRequest(src=user_id, dest=agent.agent_id)
    exchange.send(ping)
    message = exchange._transport.recv()
    assert isinstance(message, PingResponse)

    shutdown = ShutdownRequest(src=user_id, dest=agent.agent_id)
    exchange.send(shutdown)

    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
    assert not thread.is_alive()


def test_agent_action_message(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(CounterBehavior)
    user_id = exchange.user_id

    agent = Agent(
        CounterBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    thread = threading.Thread(target=agent)
    thread.start()

    value = 42
    request = ActionRequest(
        src=user_id,
        dest=agent.agent_id,
        action='add',
        pargs=(value,),
    )
    exchange.send(request)
    message = exchange._transport.recv()
    assert isinstance(message, ActionResponse)
    assert message.exception is None
    assert message.result is None

    request = ActionRequest(src=user_id, dest=agent.agent_id, action='count')
    exchange.send(request)
    message = exchange._transport.recv()
    assert isinstance(message, ActionResponse)
    assert message.exception is None
    assert message.result == value

    shutdown = ShutdownRequest(src=user_id, dest=agent.agent_id)
    exchange.send(shutdown)

    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
    assert not thread.is_alive()


def test_agent_action_message_error(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(ErrorBehavior)
    user_id = exchange.user_id

    agent = Agent(
        ErrorBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    thread = threading.Thread(target=agent)
    thread.start()

    request = ActionRequest(
        src=user_id,
        dest=agent.agent_id,
        action='fails',
    )
    exchange.send(request)
    message = exchange._transport.recv()
    assert isinstance(message, ActionResponse)
    assert isinstance(message.exception, RuntimeError)
    assert 'This action always fails.' in str(message.exception)

    shutdown = ShutdownRequest(src=user_id, dest=agent.agent_id)
    exchange.send(shutdown)

    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
    assert not thread.is_alive()


def test_agent_action_message_unknown(
    exchange: UserExchangeClient[Any],
) -> None:
    registration = exchange.register_agent(EmptyBehavior)
    user_id = exchange.user_id

    agent = Agent(
        EmptyBehavior(),
        exchange_factory=exchange.factory(),
        registration=registration,
    )
    thread = threading.Thread(target=agent)
    thread.start()

    request = ActionRequest(src=user_id, dest=agent.agent_id, action='null')
    exchange.send(request)
    message = exchange._transport.recv()
    assert isinstance(message, ActionResponse)
    assert isinstance(message.exception, AttributeError)
    assert 'null' in str(message.exception)

    shutdown = ShutdownRequest(src=user_id, dest=agent.agent_id)
    exchange.send(shutdown)

    thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
    assert not thread.is_alive()


class HandleBindingBehavior(Behavior):
    def __init__(
        self,
        unbound: UnboundRemoteHandle[EmptyBehavior],
        agent_bound: BoundRemoteHandle[EmptyBehavior],
        self_bound: BoundRemoteHandle[EmptyBehavior],
    ) -> None:
        self.unbound = unbound
        self.agent_bound = agent_bound
        self.self_bound = self_bound

    def on_setup(self) -> None:
        assert isinstance(self.unbound, BoundRemoteHandle)
        assert isinstance(self.agent_bound, BoundRemoteHandle)
        assert isinstance(self.self_bound, BoundRemoteHandle)

        assert self.unbound.mailbox_id is not None
        assert (
            self.unbound.mailbox_id
            == self.agent_bound.mailbox_id
            == self.self_bound.mailbox_id
        )

    def on_shutdown(self) -> None:
        self.unbound.close()
        self.agent_bound.close()
        self.self_bound.close()


def test_agent_run_bind_handles(exchange: UserExchangeClient[Any]) -> None:
    registration = exchange.register_agent(HandleBindingBehavior)
    behavior = HandleBindingBehavior(
        unbound=UnboundRemoteHandle(
            exchange.register_agent(EmptyBehavior).agent_id,
        ),
        agent_bound=BoundRemoteHandle(
            exchange,
            exchange.register_agent(EmptyBehavior).agent_id,
            exchange.register_agent(EmptyBehavior).agent_id,
        ),
        self_bound=BoundRemoteHandle(
            exchange,
            exchange.register_agent(EmptyBehavior).agent_id,
            registration.agent_id,
        ),
    )
    agent = Agent(
        behavior,
        exchange_factory=exchange.factory(),
        registration=registration,
    )

    agent._bind_handles()
    agent._bind_handles()  # Idempotency check
    agent.behavior.on_setup()
    agent.behavior.on_shutdown()
    # The user-bound and self-bound remote handles should be ignored.
    assert len(agent.exchange._handles) == 2  # noqa: PLR2004


class RunBehavior(Behavior):
    def __init__(self, doubler: Handle[DoubleBehavior]) -> None:
        self.doubler = doubler

    def on_shutdown(self) -> None:
        assert isinstance(self.doubler, BoundRemoteHandle)
        self.doubler.shutdown()

    @action
    def run(self, value: int) -> int:
        return self.doubler.action('double', value).result()


class DoubleBehavior(Behavior):
    @action
    def double(self, value: int) -> int:
        return 2 * value


def test_agent_to_handle_handles() -> None:
    exchange_factory = ThreadExchangeFactory()
    with exchange_factory.create_user_client() as exchange:
        runner_info = exchange.register_agent(RunBehavior)
        doubler_info = exchange.register_agent(DoubleBehavior)

        runner_handle = exchange.get_handle(runner_info.agent_id)
        doubler_handle = exchange.get_handle(doubler_info.agent_id)

        runner_behavior = RunBehavior(doubler_handle)
        doubler_behavior = DoubleBehavior()

        runner_agent = Agent(
            runner_behavior,
            exchange_factory=exchange_factory,
            registration=runner_info,
        )
        doubler_agent = Agent(
            doubler_behavior,
            exchange_factory=exchange_factory,
            registration=doubler_info,
        )

        runner_thread = threading.Thread(target=runner_agent)
        doubler_thread = threading.Thread(target=doubler_agent)

        runner_thread.start()
        doubler_thread.start()

        future: Future[int] = runner_handle.action('run', 1)
        assert future.result() == 2  # noqa: PLR2004

        runner_handle.shutdown()

        runner_thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
        doubler_thread.join(timeout=TEST_THREAD_JOIN_TIMEOUT)
