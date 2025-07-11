from __future__ import annotations

import asyncio
import pickle
from typing import Any

import pytest

from academy.exception import AgentTerminatedError
from academy.exception import HandleClosedError
from academy.exception import HandleNotBoundError
from academy.exchange import UserExchangeClient
from academy.exchange.local import LocalExchangeTransport
from academy.exchange.transport import MailboxStatus
from academy.handle import ProxyHandle
from academy.handle import RemoteHandle
from academy.handle import UnboundRemoteHandle
from academy.manager import Manager
from academy.message import Message
from academy.message import PingRequest
from testing.agents import CounterAgent
from testing.agents import EmptyAgent
from testing.agents import ErrorAgent
from testing.agents import SleepAgent
from testing.constant import TEST_SLEEP_INTERVAL


@pytest.mark.asyncio
async def test_proxy_handle_protocol() -> None:
    agent = EmptyAgent()
    handle = ProxyHandle(agent)
    assert str(agent) in str(handle)
    assert repr(agent) in repr(handle)
    assert await handle.ping() >= 0
    await handle.shutdown()


@pytest.mark.asyncio
async def test_proxy_handle_actions() -> None:
    handle = ProxyHandle(CounterAgent())

    # Via Handle.action()
    add_future: asyncio.Future[None] = await handle.action('add', 1)
    await add_future
    count_future: asyncio.Future[int] = await handle.action('count')
    assert await count_future == 1

    # Via attribute lookup
    add_future = await handle.add(1)
    await add_future
    count_future = await handle.count()
    assert await count_future == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_proxy_handle_action_errors() -> None:
    handle = ProxyHandle(ErrorAgent())

    fails_future: asyncio.Future[None] = await handle.action('fails')
    with pytest.raises(RuntimeError, match='This action always fails.'):
        await fails_future

    null_future: asyncio.Future[None] = await handle.action('null')
    with pytest.raises(AttributeError, match='null'):
        await null_future

    with pytest.raises(AttributeError, match='null'):
        await handle.null()  # type: ignore[attr-defined]

    handle.agent.foo = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match='not a method'):
        await handle.foo()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_proxy_handle_closed_errors() -> None:
    handle = ProxyHandle(EmptyAgent())
    await handle.close()

    with pytest.raises(HandleClosedError):
        await handle.action('test')
    with pytest.raises(HandleClosedError):
        await handle.ping()
    with pytest.raises(HandleClosedError):
        await handle.shutdown()


@pytest.mark.asyncio
async def test_proxy_handle_agent_shutdown_errors() -> None:
    handle = ProxyHandle(EmptyAgent())
    await handle.shutdown()

    with pytest.raises(AgentTerminatedError):
        await handle.action('test')
    with pytest.raises(AgentTerminatedError):
        await handle.ping()
    with pytest.raises(AgentTerminatedError):
        await handle.shutdown()


@pytest.mark.asyncio
async def test_unbound_remote_handle_serialize(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = UnboundRemoteHandle(registration.agent_id)

    dumped = pickle.dumps(handle)
    reconstructed = pickle.loads(dumped)
    assert isinstance(reconstructed, UnboundRemoteHandle)
    assert str(reconstructed) == str(handle)
    assert repr(reconstructed) == repr(handle)


@pytest.mark.asyncio
async def test_unbound_remote_handle_bind(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = UnboundRemoteHandle(registration.agent_id)
    with pytest.raises(
        RuntimeError,
        match='An unbound handle has no client ID.',
    ):
        _ = handle.client_id
    async with handle.bind_to_client(exchange_client) as agent_bound:
        assert isinstance(agent_bound, RemoteHandle)
        assert isinstance(agent_bound.clone(), UnboundRemoteHandle)


@pytest.mark.asyncio
async def test_unbound_remote_handle_errors(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = UnboundRemoteHandle(registration.agent_id)
    with pytest.raises(HandleNotBoundError):
        await handle.action('foo')
    with pytest.raises(HandleNotBoundError):
        await handle.ping()
    with pytest.raises(HandleNotBoundError):
        await handle.close()
    with pytest.raises(HandleNotBoundError):
        await handle.shutdown()


@pytest.mark.asyncio
async def test_remote_handle_closed_error(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = RemoteHandle(exchange_client, registration.agent_id)
    await handle.close()
    assert handle.closed()

    assert handle.client_id is not None
    with pytest.raises(HandleClosedError):
        await handle.action('foo')
    with pytest.raises(HandleClosedError):
        await handle.ping()
    with pytest.raises(HandleClosedError):
        await handle.shutdown()


@pytest.mark.asyncio
async def test_agent_remote_handle_serialize(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    async with RemoteHandle(exchange_client, registration.agent_id) as handle:
        # Note: don't call pickle.dumps here because ThreadExchange
        # is not pickleable so we test __reduce__ directly.
        class_, args = handle.__reduce__()
        reconstructed = class_(*args)
        assert isinstance(reconstructed, UnboundRemoteHandle)
        assert str(reconstructed) != str(handle)
        assert repr(reconstructed) != repr(handle)
        assert reconstructed.agent_id == handle.agent_id


@pytest.mark.asyncio
async def test_agent_remote_handle_bind(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    factory = exchange_client.factory()

    async def _handler(_: Any) -> None:  # pragma: no cover
        pass

    async with await factory.create_agent_client(
        registration,
        request_handler=_handler,
    ) as client:
        with pytest.raises(
            ValueError,
            match='Cannot create handle to self.',
        ):
            client.get_handle(registration.agent_id)


@pytest.mark.asyncio
async def test_client_remote_handle_ping_timeout(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = RemoteHandle(exchange_client, registration.agent_id)
    with pytest.raises(TimeoutError):
        await handle.ping(timeout=TEST_SLEEP_INTERVAL)


@pytest.mark.asyncio
async def test_client_remote_handle_log_bad_response(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(EmptyAgent())
    # Should log two messages but not crash:
    #   - User client got an unexpected ping request from agent client
    #   - Agent client got an unexpected ping response (containing an
    #     error produced by user) with no corresponding handle to
    #     send the response to.
    await handle.exchange.send(
        Message.create(
            src=handle.agent_id,
            dest=handle.client_id,
            body=PingRequest(),
        ),
    )
    assert await handle.ping() > 0
    await handle.shutdown()


@pytest.mark.asyncio
async def test_client_remote_handle_actions(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(CounterAgent())
    assert await handle.ping() > 0

    future: asyncio.Future[None] = await handle.action('add', 1)
    await future
    count_future: asyncio.Future[int] = await handle.action('count')
    assert await count_future == 1

    future = await handle.add(1)
    await future
    count_future = await handle.count()
    assert await count_future == 2  # noqa: PLR2004

    await handle.shutdown()


@pytest.mark.parametrize('terminate', (True, False))
@pytest.mark.asyncio
async def test_client_remote_shutdown_termination(
    terminate: bool,
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(EmptyAgent())
    await handle.shutdown(terminate=terminate)
    await manager.wait({handle})
    status = await manager.exchange_client.status(handle.agent_id)
    if terminate:
        assert status == MailboxStatus.TERMINATED
    else:
        assert status == MailboxStatus.ACTIVE


@pytest.mark.asyncio
async def test_client_remote_handle_errors(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(ErrorAgent())
    action_future = await handle.fails()
    with pytest.raises(
        RuntimeError,
        match='This action always fails.',
    ):
        await action_future

    null_future: asyncio.Future[None] = await handle.action('null')
    with pytest.raises(AttributeError, match='null'):
        await null_future

    await handle.shutdown()


@pytest.mark.asyncio
async def test_client_remote_handle_wait_futures(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(SleepAgent())
    sleep_future = await handle.sleep(TEST_SLEEP_INTERVAL)
    await handle.close(wait_futures=True)
    await sleep_future

    # Create a new, non-closed handle to shutdown the agent
    shutdown_handle = manager.get_handle(handle.agent_id)
    await shutdown_handle.shutdown()
    await manager.wait({handle.agent_id})


@pytest.mark.asyncio
async def test_client_remote_handle_cancel_futures(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(SleepAgent())
    sleep_future = await handle.sleep(TEST_SLEEP_INTERVAL)
    await handle.close(wait_futures=False)

    with pytest.raises(asyncio.CancelledError):
        await sleep_future

    # Create a new, non-closed handle to shutdown the agent
    async with manager.get_handle(handle.agent_id) as shutdown_handle:
        await shutdown_handle.shutdown()
    await manager.wait({handle.agent_id})
