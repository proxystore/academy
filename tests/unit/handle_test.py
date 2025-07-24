from __future__ import annotations

import asyncio
import logging
import pickle

import pytest

from academy.exception import AgentTerminatedError
from academy.exception import ExchangeClientNotFoundError
from academy.exchange import LocalExchangeFactory
from academy.exchange import LocalExchangeTransport
from academy.exchange import UserExchangeClient
from academy.exchange.transport import MailboxStatus
from academy.handle import exchange_context
from academy.handle import Handle
from academy.handle import ProxyHandle
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.manager import Manager
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import PingRequest
from academy.message import ShutdownRequest
from testing.agents import CounterAgent
from testing.agents import EmptyAgent
from testing.agents import ErrorAgent
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
    await handle.action('add', 1)
    count: int = await handle.action('count')
    assert count == 1

    # Via attribute lookup
    await handle.add(1)
    count = await handle.count()
    assert count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_proxy_handle_action_errors() -> None:
    handle = ProxyHandle(ErrorAgent())

    with pytest.raises(RuntimeError, match='This action always fails.'):
        await handle.action('fails')

    with pytest.raises(AttributeError, match='null'):
        await handle.action('null')

    with pytest.raises(AttributeError, match='null'):
        await handle.null()  # type: ignore[attr-defined]

    handle.agent.foo = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match='not a method'):
        await handle.foo()  # type: ignore[attr-defined]


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
async def test_agent_proxy_handle_serialize() -> None:
    agent = EmptyAgent()
    handle = ProxyHandle(agent)
    reconstructed = pickle.loads(pickle.dumps(handle))
    assert isinstance(reconstructed, ProxyHandle)
    assert type(reconstructed.agent) is EmptyAgent
    assert str(reconstructed) == str(handle)
    assert repr(reconstructed) == repr(handle)
    assert reconstructed.agent_id != handle.agent_id


@pytest.mark.asyncio
async def test_agent_handle_serialize(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(registration.agent_id)
    reconstructed = pickle.loads(pickle.dumps(handle))
    assert isinstance(reconstructed, Handle)
    assert reconstructed.agent_id == handle.agent_id
    assert str(reconstructed) == str(handle)
    assert repr(reconstructed) == repr(handle)


@pytest.mark.asyncio
async def test_agent_handle_context() -> None:
    # We cannot use the fixture here because the fixture will create context
    factory = LocalExchangeFactory()
    exchange_client = await factory.create_user_client()
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(registration.agent_id)

    with pytest.raises(ExchangeClientNotFoundError):
        assert handle.exchange is exchange_client

    exchange_context.set(exchange_client)
    assert handle.exchange is exchange_client

    await exchange_client.close()


@pytest.mark.asyncio
async def test_handle_exchange_registration(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    assert len(exchange_client._handles) == 0

    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(registration.agent_id)

    assert handle.exchange is exchange_client
    assert len(exchange_client._handles) == 0

    handle._register_with_exchange(exchange_client)
    assert len(handle._registered_exchanges) == 1
    assert len(exchange_client._handles) == 1

    # Registration is idempotent
    handle._register_with_exchange(exchange_client)
    assert len(handle._registered_exchanges) == 1
    assert len(exchange_client._handles) == 1


@pytest.mark.asyncio
async def test_agent_handle_reuse(
    manager: Manager[LocalExchangeTransport],
) -> None:
    exchange_client = manager.exchange_client
    factory = exchange_client.factory()
    destination = await manager.launch(CounterAgent)

    handle = Handle(destination.agent_id)
    assert handle.exchange is exchange_client, 'Client not inferred.'
    assert handle.handle_id not in exchange_client._handles

    assert await handle.ping() > 0
    assert handle.handle_id in exchange_client._handles
    assert exchange_client in handle._registered_exchanges

    async with await factory.create_user_client() as new_client:
        # Exchange is updated in the agent
        assert handle.exchange is new_client
        assert await handle.ping() > 0
        assert handle.handle_id in new_client._handles
        assert new_client in handle._registered_exchanges

    # Exchange is reset after
    assert handle.exchange is exchange_client

    await exchange_client.close()


@pytest.mark.asyncio
async def test_client_handle_ping_timeout(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(registration.agent_id)
    with pytest.raises(TimeoutError):
        await handle.ping(timeout=TEST_SLEEP_INTERVAL)


@pytest.mark.asyncio
async def test_client_handle_log_bad_response(
    manager: Manager[LocalExchangeTransport],
    caplog,
) -> None:
    handle = await manager.launch(EmptyAgent())
    # Should log two messages but not crash:
    #   - User client got an unexpected ping request from agent client
    #   - Agent client got an unexpected ping response (containing an
    #     error produced by user) with no corresponding handle to
    #     send the response to.
    with caplog.at_level(logging.WARNING):
        await handle.exchange.send(
            Message.create(
                src=handle.agent_id,
                dest=handle.exchange.client_id,
                body=PingRequest(),
            ),
        )
        assert await handle.ping() > 0
        await handle.shutdown()
        await asyncio.sleep(0.1)  # Wait for logs to be written
    assert 'no corresponding handle exists' in caplog.text


@pytest.mark.asyncio
async def test_client_handle_shutdown_ignore_already_termiated_error() -> None:
    handle: Handle[EmptyAgent] = Handle(AgentId.new())

    request = Message.create(
        src=UserId.new(),
        dest=handle.agent_id,
        body=ShutdownRequest(),
    )
    handle._shutdown_requests.add(request.tag)
    response = request.create_response(
        ErrorResponse(exception=AgentTerminatedError(handle.agent_id)),
    )
    await handle._process_response(response)


@pytest.mark.asyncio
async def test_client_handle_shutdown_log_error_response(caplog) -> None:
    handle: Handle[EmptyAgent] = Handle(AgentId.new())

    request = Message.create(
        src=AgentId.new(),
        dest=handle.agent_id,
        body=ShutdownRequest(),
    )
    handle._shutdown_requests.add(request.tag)
    response = request.create_response(
        ErrorResponse(exception=AgentTerminatedError(AgentId.new())),
    )

    with caplog.at_level(logging.ERROR):
        await handle._process_response(response)
    assert f'Failure requesting shutdown for {handle.agent_id}' in caplog.text


@pytest.mark.asyncio
async def test_client_handle_actions(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(CounterAgent())
    assert await handle.ping() > 0

    await handle.action('add', 1)
    count: int = await handle.action('count')
    assert count == 1

    await handle.add(1)
    count = await handle.count()
    assert count == 2  # noqa: PLR2004

    await handle.shutdown()


@pytest.mark.parametrize('terminate', (True, False))
@pytest.mark.asyncio
async def test_client_shutdown_termination(
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
async def test_client_handle_errors(
    manager: Manager[LocalExchangeTransport],
) -> None:
    handle = await manager.launch(ErrorAgent())
    with pytest.raises(
        RuntimeError,
        match='This action always fails.',
    ):
        await handle.fails()

    with pytest.raises(AttributeError, match='null'):
        await handle.action('null')

    await handle.shutdown()


@pytest.mark.asyncio
async def test_handle_default_exchange() -> None:
    factory = LocalExchangeFactory()
    exchange_client = await factory.create_user_client()
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(registration.agent_id, exchange=exchange_client)

    assert handle.exchange is exchange_client
    assert repr(exchange_client) in repr(handle)

    async with await factory.create_user_client() as new_client:
        assert handle.exchange is new_client

    assert handle.exchange is exchange_client

    await exchange_client.close()


@pytest.mark.asyncio
async def test_handle_ignore_context() -> None:
    factory = LocalExchangeFactory()
    exchange_client = await factory.create_user_client()
    registration = await exchange_client.register_agent(EmptyAgent)
    handle = Handle(
        registration.agent_id,
        exchange=exchange_client,
        ignore_context=True,
    )

    assert handle.exchange is exchange_client
    assert repr(exchange_client) in repr(handle)

    async with await factory.create_user_client():
        assert handle.exchange is exchange_client

    with pytest.raises(pickle.PicklingError):
        pickle.dumps(handle)

    await exchange_client.close()


@pytest.mark.asyncio
async def test_handle_ignore_context_error(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    with pytest.raises(ValueError, match='no explicit exchange'):
        Handle(registration.agent_id, ignore_context=True)
