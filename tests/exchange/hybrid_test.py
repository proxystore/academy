from __future__ import annotations

import logging
import pickle
import uuid
from typing import Any
from unittest import mock

import pytest

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange.hybrid import base32_to_uuid
from academy.exchange.hybrid import BoundHybridExchangeClient
from academy.exchange.hybrid import UnboundHybridExchangeClient
from academy.exchange.hybrid import uuid_to_base32
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from academy.socket import open_port
from testing.behavior import EmptyBehavior
from testing.constant import TEST_CONNECTION_TIMEOUT
from testing.constant import TEST_SLEEP
from testing.constant import TEST_THREAD_JOIN_TIMEOUT


def test_open_close_exchange(mock_redis) -> None:
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        assert isinstance(repr(exchange), str)
        assert isinstance(str(exchange), str)


def test_serialize_exchange(mock_redis) -> None:
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        dumped = pickle.dumps(exchange)
        reconstructed = pickle.loads(dumped)
        assert isinstance(reconstructed, UnboundHybridExchangeClient)


def test_key_namespaces(mock_redis) -> None:
    namespace = 'foo'
    uid = ClientId.new()
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
        namespace=namespace,
    ).bind_as_client() as exchange:
        assert isinstance(exchange, BoundHybridExchangeClient)

        assert exchange._address_key(uid).startswith(f'{namespace}:')
        assert exchange._status_key(uid).startswith(f'{namespace}:')
        assert exchange._queue_key(uid).startswith(f'{namespace}:')


def test_send_bad_identifier(mock_redis) -> None:
    uid = ClientId.new()
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        message = PingRequest(src=exchange.mailbox_id, dest=uid)
        with pytest.raises(BadEntityIdError):
            exchange.send(uid, message)


def test_send_mailbox_closed(mock_redis) -> None:
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        uid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(uid)
        message = PingRequest(src=exchange.mailbox_id, dest=uid)
        with pytest.raises(MailboxClosedError):
            exchange.send(uid, message)


def test_create_mailbox_bad_identifier(mock_redis) -> None:
    uid: AgentId[Any] = AgentId.new()
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=uid)


def test_send_to_mailbox_direct(mock_redis, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    exchange = UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    )
    with exchange.bind_as_client() as client_1:
        with exchange.bind_as_client(start_listener=False) as client_2:
            message = PingRequest(
                src=client_1.mailbox_id,
                dest=client_2.mailbox_id,
            )
            for _ in range(3):
                client_1.send(client_2.mailbox_id, message)
                assert (
                    client_2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message
                )


def test_send_to_mailbox_indirect(mock_redis, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    messages = 3
    exchange = UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    )

    with exchange.bind_as_client() as client_1:
        aid = client_1.register_agent(EmptyBehavior)
        message = PingRequest(src=client_1.mailbox_id, dest=aid)
        for _ in range(messages):
            client_1.send(aid, message)

    with exchange.bind_as_agent(agent_id=aid) as mailbox:
        for _ in range(messages):
            assert mailbox.recv(timeout=TEST_CONNECTION_TIMEOUT) == message


@pytest.mark.skip(reason='Not implemented. Need async for implementation.')
def test_mailbox_recv_closed(mock_redis) -> None:  # pragma: no cover
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)

        with exchange.clone().bind_as_agent(agent_id=aid) as mailbox:
            exchange.terminate(aid)

            with pytest.raises(MailboxClosedError):
                mailbox.recv(timeout=TEST_SLEEP)


def test_mailbox_create_terminated(mock_redis) -> None:
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        aid = exchange.register_agent(EmptyBehavior)
        exchange.terminate(aid)

        with pytest.raises(BadEntityIdError):
            exchange.clone().bind_as_agent(agent_id=aid)


def test_mailbox_redis_error_logging(mock_redis, caplog) -> None:
    caplog.set_level(logging.ERROR)
    with mock.patch(
        'academy.exchange.hybrid.BoundHybridExchangeClient._pull_messages_from_redis',
        side_effect=RuntimeError('Mock thread error.'),
    ):
        with UnboundHybridExchangeClient(
            redis_host='localhost',
            redis_port=0,
        ).bind_as_client() as exchange:
            assert isinstance(exchange, BoundHybridExchangeClient)
            exchange._redis_thread.join(TEST_THREAD_JOIN_TIMEOUT)
            assert any(
                f'Error in redis watcher thread for {exchange.mailbox_id}'
                in record.message
                for record in caplog.records
                if record.levelname == 'ERROR'
            )


def test_send_to_mailbox_bad_cached_address(mock_redis) -> None:
    port1, port2 = open_port(), open_port()
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as client1:
        assert isinstance(client1, BoundHybridExchangeClient)

        aid = client1.register_agent(EmptyBehavior)

        with UnboundHybridExchangeClient(
            redis_host='localhost',
            redis_port=0,
            ports=[port1],
        ).bind_as_agent(agent_id=aid) as client2:
            message = PingRequest(
                src=client1.mailbox_id,
                dest=client2.mailbox_id,
            )
            client1.send(client2.mailbox_id, message)
            assert client2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message

        # Address of mailbox is now in the exchanges cache but
        # the mailbox is no longer listening on that address.
        address = client1._address_cache[client2.mailbox_id]
        socket = client1._socket_pool._sockets[address]
        socket.close()

        with UnboundHybridExchangeClient(
            redis_host='localhost',
            redis_port=0,
            ports=[port2],
        ).bind_as_agent(agent_id=aid) as client2:
            # This send will try the cached address, fail, catch the error,
            # and retry via redis.
            client1.send(client2.mailbox_id, message)
            assert client2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message


class A(Behavior): ...


class B(Behavior): ...


class C(B): ...


def test_exchange_discover(mock_redis) -> None:
    with UnboundHybridExchangeClient(
        redis_host='localhost',
        redis_port=0,
    ).bind_as_client() as exchange:
        bid = exchange.register_agent(B)
        cid = exchange.register_agent(C)
        did = exchange.register_agent(C)
        exchange.terminate(did)

        assert len(exchange.discover(A)) == 0
        assert exchange.discover(B, allow_subclasses=False) == (bid,)
        assert exchange.discover(B, allow_subclasses=True) == (bid, cid)


def test_uuid_encoding() -> None:
    for _ in range(3):
        uid = uuid.uuid4()
        encoded = uuid_to_base32(uid)
        assert base32_to_uuid(encoded) == uid
