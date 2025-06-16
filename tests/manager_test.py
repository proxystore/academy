from __future__ import annotations

import time

import pytest

from academy.exception import BadEntityIdError
from academy.exchange.thread import ThreadExchangeFactory
from academy.launcher import ThreadLauncher
from academy.manager import Manager
from academy.message import PingRequest
from academy.message import PingResponse
from testing.behavior import EmptyBehavior
from testing.behavior import SleepBehavior
from testing.constant import TEST_LOOP_SLEEP
from testing.constant import TEST_THREAD_JOIN_TIMEOUT


def test_protocol() -> None:
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        assert isinstance(repr(manager), str)
        assert isinstance(str(manager), str)


def test_basic_usage() -> None:
    behavior = SleepBehavior(TEST_LOOP_SLEEP)
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        manager.launch(behavior)
        manager.launch(behavior)

        time.sleep(5 * TEST_LOOP_SLEEP)


def test_reply_to_requests_with_error() -> None:
    exchange = ThreadExchangeFactory()
    with Manager(
        exchange=exchange,
        launcher=ThreadLauncher(),
    ) as manager:
        with exchange.create_user_client(start_listener=False) as client:
            request = PingRequest(
                src=client.user_id,
                dest=manager.mailbox_id,
            )
            client.send(request.dest, request)
            response = client._transport.recv()
            assert isinstance(response, PingResponse)
            assert isinstance(response.exception, TypeError)


def test_wait_bad_identifier(exchange: ThreadExchangeFactory) -> None:
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        agent_id = manager.exchange.register_agent(EmptyBehavior)

        with pytest.raises(BadEntityIdError):
            manager.wait(agent_id)


def test_wait_timeout(exchange: ThreadExchangeFactory) -> None:
    behavior = SleepBehavior(TEST_LOOP_SLEEP)
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        handle = manager.launch(behavior)

        with pytest.raises(TimeoutError):
            manager.wait(handle.agent_id, timeout=TEST_LOOP_SLEEP)


def test_shutdown_bad_identifier(
    exchange: ThreadExchangeFactory,
) -> None:
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        agent_id = manager.exchange.register_agent(EmptyBehavior)

        with pytest.raises(BadEntityIdError):
            manager.shutdown(agent_id)


def test_shutdown_nonblocking(exchange: ThreadExchangeFactory) -> None:
    behavior = SleepBehavior(TEST_LOOP_SLEEP)
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        handle = manager.launch(behavior)
        manager.shutdown(handle.agent_id, blocking=False)
        manager.wait(handle.agent_id, timeout=TEST_THREAD_JOIN_TIMEOUT)


def test_shutdown_blocking(exchange: ThreadExchangeFactory) -> None:
    behavior = SleepBehavior(TEST_LOOP_SLEEP)
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher=ThreadLauncher(),
    ) as manager:
        handle = manager.launch(behavior)
        manager.shutdown(handle.agent_id, blocking=True)
        manager.wait(handle.agent_id, timeout=TEST_LOOP_SLEEP)


def test_bad_default_launcher() -> None:
    with pytest.raises(ValueError, match='No launcher named "second"'):
        Manager(
            exchange=ThreadExchangeFactory(),
            launcher={'first': ThreadLauncher()},
            default_launcher='second',
        )


def test_add_and_set_launcher_errors() -> None:
    launcher = ThreadLauncher()
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher={'first': launcher},
    ) as manager:
        with pytest.raises(
            ValueError,
            match='Launcher named "first" already exists.',
        ):
            manager.add_launcher('first', launcher)
        with pytest.raises(
            ValueError,
            match='A launcher name "second" does not exist.',
        ):
            manager.set_default_launcher('second')


def test_multiple_launcher() -> None:
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher={'first': ThreadLauncher()},
    ) as manager:
        manager.launch(EmptyBehavior(), launcher='first')

        manager.add_launcher('second', ThreadLauncher())
        manager.set_default_launcher('second')
        manager.launch(EmptyBehavior())
        manager.launch(EmptyBehavior(), launcher='first')


def test_multiple_launcher_no_default() -> None:
    with Manager(
        exchange=ThreadExchangeFactory(),
        launcher={'first': ThreadLauncher()},
    ) as manager:
        with pytest.raises(ValueError, match='no default is set.'):
            manager.launch(EmptyBehavior())
