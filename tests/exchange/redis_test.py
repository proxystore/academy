from __future__ import annotations

import pickle
from typing import Any

import pytest

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.exchange.redis import RedisExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from testing.behavior import EmptyBehavior


@pytest.mark.asyncio
async def test_basic_usage(mock_redis) -> None:
    unbound_exchange = RedisExchangeFactory('localhost', port=0)
    assert isinstance(unbound_exchange, ExchangeFactory)

    with unbound_exchange.bind_as_client() as exchange:
        assert isinstance(exchange, ExchangeClient)
        assert isinstance(repr(exchange), str)
        assert isinstance(str(exchange), str)

        aid = await exchange.register_agent(EmptyBehavior)
        await exchange.register_agent(
            EmptyBehavior,
            agent_id=aid,
        )  # Idempotency check
        assert isinstance(aid, AgentId)

        with unbound_exchange.bind_as_agent(agent_id=aid) as mailbox:
            for _ in range(3):
                message = PingRequest(src=exchange.mailbox_id, dest=aid)
                await exchange.send(aid, message)
                assert await mailbox.recv() == message

        await exchange.terminate(aid)
        await exchange.terminate(aid)  # Idempotency check


@pytest.mark.asyncio
async def test_bad_identifier_error(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        uid = ClientId.new()
        with pytest.raises(BadEntityIdError):
            await exchange.send(
                uid,
                PingRequest(src=exchange.mailbox_id, dest=uid)
            )


@pytest.mark.asyncio
async def test_mailbox_closed_error(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        aid = await exchange.register_agent(EmptyBehavior)
        with exchange.clone().bind_as_agent(agent_id=aid) as mailbox:
            await exchange.terminate(aid)
            with pytest.raises(MailboxClosedError):
                await exchange.send(aid, PingRequest(src=aid, dest=aid))
            with pytest.raises(MailboxClosedError):
                await mailbox.recv()


@pytest.mark.asyncio
async def test_mailbox_terminated(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        aid = await exchange.register_agent(EmptyBehavior)
        await exchange.terminate(aid)
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)


def test_mailbox_non_existent(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        aid: AgentId[Any] = AgentId.new()
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)


@pytest.mark.asyncio
async def test_get_handle_to_client(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        aid = await exchange.register_agent(EmptyBehavior)
        handle: BoundRemoteHandle[Any] = exchange.get_handle(aid)
        handle.close()

        with pytest.raises(TypeError, match='Handle must be created from an'):
            exchange.get_handle(ClientId.new())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mailbox_timeout(mock_redis) -> None:
    with RedisExchangeFactory('localhost', port=0).bind_as_client(
        start_listener=False,
    ) as exchange:
        with pytest.raises(TimeoutError):
            await exchange.recv(timeout=0.001)


def test_exchange_serialization(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        pickled = pickle.dumps(exchange)
        reconstructed = pickle.loads(pickled)
        assert isinstance(reconstructed, ExchangeFactory)


class A(Behavior): ...


class B(Behavior): ...


class C(B): ...


@pytest.mark.asyncio
async def test_exchange_discover(mock_redis) -> None:
    with RedisExchangeFactory(
        'localhost',
        port=0,
    ).bind_as_client() as exchange:
        bid = await exchange.register_agent(B)
        cid = await exchange.register_agent(C)
        did = await exchange.register_agent(C)
        await exchange.terminate(did)

        assert len(exchange.discover(A)) == 0
        assert await exchange.discover(B, allow_subclasses=False) == (bid,)
        assert await exchange.discover(B, allow_subclasses=True) == (bid, cid)
