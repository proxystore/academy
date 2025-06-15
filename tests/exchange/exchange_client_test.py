from __future__ import annotations

import pickle
from typing import Any

import pytest

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.exchange import MailboxStatus
from academy.identifier import AgentId
from academy.message import PingRequest
from testing.behavior import EmptyBehavior

# These fixtures are defined in testing/exchange.py
EXCHANGE_FACTORY_FIXTURES = (
    'http_exchange_factory',
    'hybrid_exchange_factory',
    'redis_exchange_factory',
    'thread_exchange_factory',
)


@pytest.fixture(params=EXCHANGE_FACTORY_FIXTURES)
def client(request) -> ExchangeClient:
    factory = request.getfixturevalue(request.param)
    with factory._create_client() as client:
        yield client


def test_client_create_factory(client: ExchangeClient) -> None:
    new_factory = client.factory()
    assert isinstance(new_factory, ExchangeFactory)


def test_client_register_agent(client: ExchangeClient) -> None:
    agent_id = client.register_agent(EmptyBehavior)
    assert client.status(agent_id) == MailboxStatus.ACTIVE


def test_client_send_recv(client: ExchangeClient) -> None:
    for _ in range(3):
        message = PingRequest(
            src=client.mailbox_id,
            dest=client.mailbox_id,
        )
        client.send(client.mailbox_id, message)
        assert client.recv() == message


def test_client_send_bad_identifier_error(client: ExchangeClient) -> None:
    uid: AgentId[Any] = AgentId.new()
    with pytest.raises(BadEntityIdError):
        client.send(uid, PingRequest(src=client.mailbox_id, dest=uid))


def test_client_send_mailbox_closed(client: ExchangeClient) -> None:
    aid = client.register_agent(EmptyBehavior)
    client.terminate(aid)
    with pytest.raises(MailboxClosedError):
        client.send(aid, PingRequest(src=client.mailbox_id, dest=aid))


def test_client_recv_mailbox_closed(client: ExchangeClient) -> None:
    client.terminate(client.mailbox_id)
    with pytest.raises(MailboxClosedError):
        client.recv()


def test_client_recv_timeout(client: ExchangeClient) -> None:
    with pytest.raises(TimeoutError):
        assert client.recv(timeout=0.001)


def test_client_non_pickleable(client: ExchangeClient) -> None:
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(client)


class A(Behavior): ...


class B(Behavior): ...


class C(B): ...


def test_client_discover(client: ExchangeClient) -> None:
    bid = client.register_agent(B)
    cid = client.register_agent(C)
    did = client.register_agent(C)
    client.terminate(did)

    assert len(client.discover(A)) == 0
    assert client.discover(B, allow_subclasses=False) == (bid,)
    assert client.discover(B, allow_subclasses=True) == (bid, cid)
