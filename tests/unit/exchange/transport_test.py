from __future__ import annotations

import pickle
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from academy.agent import Agent
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange import ExchangeFactory
from academy.exchange import MailboxStatus
from academy.exchange.hybrid import HybridExchangeFactory
from academy.exchange.transport import AgentRegistrationT
from academy.exchange.transport import ExchangeTransport
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import PingRequest
from academy.message import SuccessResponse
from testing.agents import EmptyAgent
from testing.constant import TEST_SLEEP_INTERVAL
from testing.fixture import EXCHANGE_FACTORY_TYPES


@pytest_asyncio.fixture(params=EXCHANGE_FACTORY_TYPES)
async def transport(
    request,
    get_factory,
) -> AsyncGenerator[ExchangeTransport[AgentRegistrationT]]:
    factory = get_factory(request.param)
    async with await factory._create_transport() as transport:
        yield transport


@pytest.mark.asyncio
async def test_transport_repr(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    assert isinstance(repr(transport), str)
    assert isinstance(str(transport), str)


@pytest.mark.asyncio
async def test_transport_create_factory(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    new_factory = transport.factory()
    assert isinstance(new_factory, ExchangeFactory)


@pytest.mark.asyncio
async def test_transport_register_agent(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    registration = await transport.register_agent(EmptyAgent)
    status = await transport.status(registration.agent_id)
    assert status == MailboxStatus.ACTIVE


@pytest.mark.asyncio
async def test_transport_status(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    uid = UserId.new()
    status = await transport.status(uid)
    assert status == MailboxStatus.MISSING
    registration = await transport.register_agent(EmptyAgent)
    status = await transport.status(registration.agent_id)
    assert status == MailboxStatus.ACTIVE
    await transport.terminate(registration.agent_id)
    await transport.terminate(registration.agent_id)  # Idempotency
    status = await transport.status(registration.agent_id)
    assert status == MailboxStatus.TERMINATED


@pytest.mark.asyncio
async def test_transport_send_recv(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    for _ in range(3):
        message = Message.create(
            src=transport.mailbox_id,
            dest=transport.mailbox_id,
            body=PingRequest(),
        )
        await transport.send(message)
        assert await transport.recv() == message


@pytest.mark.asyncio
async def test_transport_send_bad_identifier_error(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    uid: AgentId[Any] = AgentId.new()
    with pytest.raises(BadEntityIdError):
        await transport.send(
            Message.create(
                src=transport.mailbox_id,
                dest=uid,
                body=PingRequest(),
            ),
        )


@pytest.mark.asyncio
async def test_transport_send_mailbox_closed(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    registration = await transport.register_agent(EmptyAgent)
    await transport.terminate(registration.agent_id)
    with pytest.raises(MailboxTerminatedError):
        await transport.send(
            Message.create(
                src=transport.mailbox_id,
                dest=registration.agent_id,
                body=PingRequest(),
            ),
        )


@pytest.mark.asyncio
async def test_transport_recv_mailbox_closed(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    await transport.terminate(transport.mailbox_id)
    with pytest.raises(MailboxTerminatedError):
        await transport.recv()


@pytest.mark.asyncio
async def test_transport_recv_timeout(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    with pytest.raises(TimeoutError):
        await transport.recv(timeout=TEST_SLEEP_INTERVAL)


@pytest.mark.asyncio
async def test_transport_terminate_unknown_ok(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    await transport.terminate(UserId.new())


@pytest.mark.parametrize('factory_type', EXCHANGE_FACTORY_TYPES)
async def test_transport_terminate_reply_pending_requests(
    factory_type: type[ExchangeFactory[Any]],
    get_factory,
) -> None:
    if factory_type is HybridExchangeFactory:
        pytest.skip(
            'HybridExchangeTransport termination behavior is unreliable.',
        )

    factory = get_factory(factory_type)
    async with await factory._create_transport() as transport1:
        async with await factory._create_transport() as transport2:
            # Put a request and a response message in transport2 mailbox
            message1 = Message.create(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
                body=PingRequest(),
            )
            message2 = Message.create(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
                body=SuccessResponse(),
            )
            await transport1.send(message1)
            await transport1.send(message2)

            # Terminate transport2 mailbox should reply to all *requests*
            # with an error. Responses are ignored.
            await transport2.terminate(transport2.mailbox_id)

            # Check that transport1 gets a response to its request that
            # was terminated.
            response = await transport1.recv()
            body = response.get_body()
            assert isinstance(body, ErrorResponse)
            assert response.tag == message1.tag
            assert isinstance(body.get_exception(), MailboxTerminatedError)

            # No other messages should have been received
            with pytest.raises(TimeoutError):
                await transport1.recv(timeout=TEST_SLEEP_INTERVAL)


@pytest.mark.asyncio
async def test_transport_non_pickleable(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(transport)


class A(Agent): ...


class B(Agent): ...


class C(B): ...


@pytest.mark.asyncio
async def test_transport_discover(
    transport: ExchangeTransport[AgentRegistrationT],
) -> None:
    bid = (await transport.register_agent(B)).agent_id
    cid = (await transport.register_agent(C)).agent_id
    did = (await transport.register_agent(C)).agent_id
    await transport.terminate(did)

    found = await transport.discover(A)
    assert len(found) == 0
    found = await transport.discover(B, allow_subclasses=False)
    assert found == (bid,)
    found = await transport.discover(B, allow_subclasses=True)
    assert found == (bid, cid)
