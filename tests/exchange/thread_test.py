from __future__ import annotations

import pickle
from typing import Any

import pytest

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import MailboxStatus
from academy.exchange.thread import _ThreadExchangeState
from academy.exchange.thread import ThreadExchangeClient
from academy.exchange.thread import ThreadExchangeFactory
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from testing.behavior import EmptyBehavior


def test_factory_create_client() -> None:
    with ThreadExchangeFactory().create_user_client():
        pass


def test_create_client() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client1:
        # When mailbox_id is not provided to.new(), a new ClientID should
        # be created and registered in the exchange.
        assert isinstance(client1.mailbox_id, ClientId)

    with ThreadExchangeClient(client1.mailbox_id, state=state) as client2:
        assert client1.mailbox_id == client2.mailbox_id


def test_register_agent() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        agent_id = client.register_agent(EmptyBehavior)
        assert client.status(agent_id) == MailboxStatus.ACTIVE


def test_create_factory() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        factory = client.factory()
        assert isinstance(factory, ThreadExchangeFactory)


def test_send_recv() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        for _ in range(3):
            message = PingRequest(
                src=client.mailbox_id,
                dest=client.mailbox_id,
            )
            client.send(client.mailbox_id, message)
            assert client.recv() == message


def test_send_bad_identifier_error() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        uid: AgentId[Any] = AgentId.new()
        with pytest.raises(BadEntityIdError):
            client.send(uid, PingRequest(src=client.mailbox_id, dest=uid))


def test_terminate_raises_mailbox_closed() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        aid = client.register_agent(EmptyBehavior)
        client.terminate(aid)
        with pytest.raises(MailboxClosedError):
            client.send(aid, PingRequest(src=client.mailbox_id, dest=aid))


def test_non_pickleable() -> None:
    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        with pytest.raises(pickle.PicklingError):
            pickle.dumps(client)


def test_discover() -> None:
    class A(Behavior): ...

    class B(Behavior): ...

    class C(B): ...

    state = _ThreadExchangeState()
    with ThreadExchangeClient.new(state=state) as client:
        bid = client.register_agent(B)
        cid = client.register_agent(C)
        did = client.register_agent(C)
        client.terminate(did)

        assert len(client.discover(A)) == 0
        assert client.discover(B, allow_subclasses=False) == (bid,)
        assert client.discover(B, allow_subclasses=True) == (bid, cid)
