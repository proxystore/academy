from __future__ import annotations

import logging
from typing import Any
from unittest import mock

import pytest
import requests

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange.cloud.client import HttpExchangeClient
from academy.exchange.cloud.client import HttpExchangeFactory
from academy.exchange.cloud.client import spawn_http_exchange
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from academy.socket import open_port
from testing.behavior import EmptyBehavior
from testing.constant import TEST_CONNECTION_TIMEOUT
from testing.constant import TEST_SLEEP


def test_simple_exchange_repr(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        assert isinstance(repr(exchange), str)
        assert isinstance(str(exchange), str)


def test_create_terminate(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.register_agent(
            EmptyBehavior,
            agent_id=aid,
        )  # Idempotency check
        exchange.terminate(aid)
        exchange.terminate(aid)  # Idempotency check


def test_create_mailbox_bad_identifier(
    http_exchange_server: tuple[str, int],
) -> None:
    host, port = http_exchange_server
    aid: AgentId[Any] = AgentId.new()
    with pytest.raises(BadEntityIdError):
        HttpExchangeFactory(host, port).bind_as_agent(agent_id=aid)


def test_send_and_recv(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        cid = exchange.mailbox_id
        aid = exchange.register_agent(EmptyBehavior)

        message = PingRequest(src=cid, dest=aid)
        exchange.send(aid, message)

        with exchange.clone().bind_as_agent(
            agent_id=aid,
        ) as mailbox:
            assert mailbox.recv(timeout=TEST_CONNECTION_TIMEOUT) == message


def test_send_bad_identifer(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    cid = ClientId.new()
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        message = PingRequest(src=exchange.mailbox_id, dest=cid)
        with pytest.raises(BadEntityIdError):
            exchange.send(cid, message)


def test_send_mailbox_closed(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(aid)
        message = PingRequest(src=aid, dest=aid)
        with pytest.raises(MailboxClosedError):
            exchange.send(aid, message)


def test_recv_timeout(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        assert isinstance(exchange, HttpExchangeClient)

        with mock.patch.object(
            exchange._session,
            'get',
            side_effect=requests.exceptions.Timeout,
        ):
            with pytest.raises(TimeoutError):
                assert exchange.recv(timeout=TEST_SLEEP)


def test_recv_mailbox_closed(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(aid)
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)


class A(Behavior): ...


class B(Behavior): ...


class C(B): ...


def test_exchange_discover(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(host, port).bind_as_client() as exchange:
        bid = exchange.register_agent(B)
        cid = exchange.register_agent(C)
        did = exchange.register_agent(C)
        exchange.terminate(did)

        assert len(exchange.discover(A)) == 0
        assert exchange.discover(B, allow_subclasses=False) == (bid,)
        assert exchange.discover(B, allow_subclasses=True) == (bid, cid)


def test_additional_headers(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    with HttpExchangeFactory(
        host,
        port,
        {'Authorization': 'fake auth'},
    ).bind_as_client() as exchange:
        assert isinstance(exchange, HttpExchangeClient)
        assert 'Authorization' in exchange._session.headers


def test_spawn_http_exchange() -> None:
    with spawn_http_exchange(
        'localhost',
        open_port(),
        level=logging.ERROR,
        timeout=TEST_CONNECTION_TIMEOUT,
    ) as unbound:
        with unbound.bind_as_client() as exchange:
            assert isinstance(exchange, ExchangeClient)
