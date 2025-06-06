from __future__ import annotations

import pickle
from typing import Any

import pytest

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import BoundExchangeClient
from academy.exchange import UnboundExchangeClient
from academy.exchange.thread import UnboundThreadExchangeClient
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from testing.behavior import EmptyBehavior


def test_basic_usage() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        assert isinstance(exchange, BoundExchangeClient)
        assert isinstance(repr(exchange), str)
        assert isinstance(str(exchange), str)
        assert isinstance(exchange.mailbox_id, ClientId)

        aid = exchange.register_agent(EmptyBehavior)
        exchange.register_agent(EmptyBehavior, agent_id=aid)
        assert isinstance(aid, AgentId)

        for _ in range(3):
            message = PingRequest(
                src=exchange.mailbox_id,
                dest=exchange.mailbox_id,
            )
            exchange.send(exchange.mailbox_id, message)
            assert exchange.recv() == message

        exchange.terminate(aid)
        exchange.terminate(aid)  # Idempotency check


def test_bad_identifier_error() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        uid: AgentId[Any] = AgentId.new()
        with pytest.raises(BadEntityIdError):
            exchange.send(uid, PingRequest(src=exchange.mailbox_id, dest=uid))


def test_mailbox_closed_error() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(aid)
        with pytest.raises(MailboxClosedError):
            exchange.send(aid, PingRequest(src=exchange.mailbox_id, dest=aid))


def test_get_handle_to_client() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        handle = exchange.get_handle(aid)
        handle.close()

        with pytest.raises(TypeError, match='Handle must be created from an'):
            exchange.get_handle(ClientId.new())  # type: ignore[arg-type]


def test_non_pickleable() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        with pytest.raises(pickle.PicklingError):
            pickle.dumps(exchange)


def test_discover() -> None:
    class A(Behavior): ...

    class B(Behavior): ...

    class C(B): ...

    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        bid = exchange.register_agent(B)
        cid = exchange.register_agent(C)
        did = exchange.register_agent(C)
        exchange.terminate(did)

        assert len(exchange.discover(A)) == 0
        assert exchange.discover(B, allow_subclasses=False) == (bid,)
        assert exchange.discover(B, allow_subclasses=True) == (bid, cid)


def test_exchange_clone() -> None:
    unbound_exchange = UnboundThreadExchangeClient()
    assert isinstance(unbound_exchange, UnboundExchangeClient)

    user1 = unbound_exchange.bind_as_client()

    clone = user1.clone()
    assert isinstance(clone, UnboundExchangeClient)

    user2 = clone.bind_as_client()
    # Test user 1 exists in exchange 2
    user2.send(
        user1.mailbox_id,
        PingRequest(src=user2.mailbox_id, dest=user1.mailbox_id),
    )
    # Test user 2 exists in exchange 1
    user1.send(
        user2.mailbox_id,
        PingRequest(src=user1.mailbox_id, dest=user2.mailbox_id),
    )

    user1.close()
    user2.close()


def test_mailbox_terminated() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(aid)
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)


def test_mailbox_non_existent() -> None:
    with UnboundThreadExchangeClient().bind_as_client() as exchange:
        aid: AgentId[Any] = AgentId.new()
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)
