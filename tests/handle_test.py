from __future__ import annotations

import pickle
from concurrent.futures import Future

import pytest

from academy.exception import HandleClosedError
from academy.exception import HandleNotBoundError
from academy.exception import MailboxClosedError
from academy.exchange import UserExchangeClient
from academy.exchange.thread import ThreadExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.handle import Handle
from academy.handle import ProxyHandle
from academy.handle import UnboundRemoteHandle
from academy.launcher import ThreadLauncher
from academy.message import PingRequest
from testing.behavior import CounterBehavior
from testing.behavior import EmptyBehavior
from testing.behavior import ErrorBehavior
from testing.behavior import SleepBehavior
from testing.constant import TEST_SLEEP


def test_proxy_handle_protocol() -> None:
    behavior = EmptyBehavior()
    handle = ProxyHandle(behavior)
    assert isinstance(handle, Handle)
    assert str(behavior) in str(handle)
    assert repr(behavior) in repr(handle)
    assert handle.ping() >= 0
    handle.shutdown()


def test_proxy_handle_actions() -> None:
    handle = ProxyHandle(CounterBehavior())

    # Via Handle.action()
    assert handle.action('add', 1).result() is None
    assert handle.action('count').result() == 1

    # Via attribute lookup
    assert handle.add(1).result() is None
    assert handle.count().result() == 2  # noqa: PLR2004


def test_proxy_handle_action_errors() -> None:
    handle = ProxyHandle(ErrorBehavior())
    with pytest.raises(RuntimeError, match='This action always fails.'):
        handle.action('fails').result()
    with pytest.raises(AttributeError, match='null'):
        handle.action('null').result()
    with pytest.raises(AttributeError, match='null'):
        handle.null()  # type: ignore[attr-defined]

    handle.behavior.foo = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match='not a method'):
        handle.foo()  # type: ignore[attr-defined]


def test_proxy_handle_closed_errors() -> None:
    handle = ProxyHandle(EmptyBehavior())
    handle.close()

    with pytest.raises(HandleClosedError):
        handle.action('test')
    with pytest.raises(HandleClosedError):
        handle.ping()
    with pytest.raises(HandleClosedError):
        handle.shutdown()


def test_proxy_handle_agent_shutdown_errors() -> None:
    handle = ProxyHandle(EmptyBehavior())
    handle.shutdown()

    with pytest.raises(MailboxClosedError):
        handle.action('test')
    with pytest.raises(MailboxClosedError):
        handle.ping()
    with pytest.raises(MailboxClosedError):
        handle.shutdown()


def test_unbound_remote_handle_serialize(
    exchange: UserExchangeClient,
) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    handle = UnboundRemoteHandle(agent_id)
    assert isinstance(handle, Handle)

    dumped = pickle.dumps(handle)
    reconstructed = pickle.loads(dumped)
    assert isinstance(reconstructed, UnboundRemoteHandle)
    assert str(reconstructed) == str(handle)
    assert repr(reconstructed) == repr(handle)


def test_unbound_remote_handle_bind(exchange: UserExchangeClient) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    handle = UnboundRemoteHandle(agent_id)
    with handle.bind_to_exchange(exchange) as agent_bound:
        assert isinstance(agent_bound, BoundRemoteHandle)


def test_unbound_remote_handle_errors(exchange: UserExchangeClient) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    handle = UnboundRemoteHandle(agent_id)
    request = PingRequest(src=exchange.user_id, dest=agent_id)
    with pytest.raises(HandleNotBoundError):
        handle._send_request(request)
    with pytest.raises(HandleNotBoundError):
        handle.action('foo')
    with pytest.raises(HandleNotBoundError):
        handle.ping()
    with pytest.raises(HandleNotBoundError):
        handle.close()
    with pytest.raises(HandleNotBoundError):
        handle.shutdown()


def test_remote_handle_closed_error(exchange: UserExchangeClient) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    handle = BoundRemoteHandle(
        exchange,
        agent_id,
        exchange.user_id,
    )
    handle.close()

    assert handle.mailbox_id is not None
    with pytest.raises(HandleClosedError):
        handle.action('foo')
    with pytest.raises(HandleClosedError):
        handle.ping()
    with pytest.raises(HandleClosedError):
        handle.shutdown()


def test_agent_remote_handle_serialize(exchange: UserExchangeClient) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    with BoundRemoteHandle(exchange, agent_id, exchange.user_id) as handle:
        # Note: don't call pickle.dumps here because ThreadExchange
        # is not pickleable so we test __reduce__ directly.
        class_, args = handle.__reduce__()
        reconstructed = class_(*args)
        assert isinstance(reconstructed, UnboundRemoteHandle)
        assert str(reconstructed) != str(handle)
        assert repr(reconstructed) != repr(handle)
        assert reconstructed.agent_id == handle.agent_id


def test_agent_remote_handle_bind(exchange: UserExchangeClient) -> None:
    agent_id = exchange.register_agent(EmptyBehavior)
    with exchange.factory().create_agent_client(
        agent_id=agent_id,
        request_handler=lambda _: None,
    ) as client:
        with pytest.raises(
            ValueError,
            match=f'Cannot create handle to {agent_id}',
        ):
            client.get_handle(agent_id)


def test_client_remote_handle_log_bad_response(
    launcher: ThreadLauncher,
) -> None:
    behavior = EmptyBehavior()
    with ThreadExchangeFactory().create_user_client() as exchange:
        with launcher.launch(behavior, exchange) as handle:
            assert handle.mailbox_id is not None
            # Should log but not crash
            handle.exchange.send(
                handle.mailbox_id,
                PingRequest(src=handle.agent_id, dest=handle.mailbox_id),
            )
            assert handle.ping() > 0

            handle.shutdown()


def test_client_remote_handle_actions(
    launcher: ThreadLauncher,
) -> None:
    behavior = CounterBehavior()
    with ThreadExchangeFactory().create_user_client() as exchange:
        with launcher.launch(behavior, exchange) as handle:
            assert handle.ping() > 0

            handle.action('add', 1).result()
            count_future: Future[int] = handle.action('count')
            assert count_future.result() == 1

            handle.add(1).result()
            count_future = handle.count()
            assert count_future.result() == 2  # noqa: PLR2004

            handle.shutdown()


def test_client_remote_handle_errors(
    launcher: ThreadLauncher,
) -> None:
    behavior = ErrorBehavior()
    with ThreadExchangeFactory().create_user_client() as exchange:
        with launcher.launch(behavior, exchange) as handle:
            with pytest.raises(
                RuntimeError,
                match='This action always fails.',
            ):
                handle.action('fails').result()
            with pytest.raises(AttributeError, match='null'):
                handle.action('null').result()

            handle.shutdown()


def test_client_remote_handle_wait_futures(
    launcher: ThreadLauncher,
) -> None:
    with ThreadExchangeFactory().create_user_client() as exchange:
        behavior = SleepBehavior()
        handle = launcher.launch(behavior, exchange)

        future: Future[None] = handle.action('sleep', TEST_SLEEP)
        handle.close(wait_futures=True)
        future.result(timeout=0)

        # Still need to shutdown agent to exit properly
        handle = exchange.get_handle(handle.agent_id)
        handle.shutdown()


def test_client_remote_handle_cancel_futures(
    launcher: ThreadLauncher,
) -> None:
    with ThreadExchangeFactory().create_user_client() as exchange:
        behavior = SleepBehavior()
        handle = launcher.launch(behavior, exchange)

        future: Future[None] = handle.action('sleep', TEST_SLEEP)
        handle.close(wait_futures=False)
        assert future.cancelled()

        # Still need to shutdown agent to exit properly
        handle = exchange.get_handle(handle.agent_id)
        handle.shutdown()
