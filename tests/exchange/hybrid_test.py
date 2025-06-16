from __future__ import annotations

import logging
import pickle
import uuid
from unittest import mock

from academy.exchange.hybrid import base32_to_uuid
from academy.exchange.hybrid import HybridExchangeFactory
from academy.exchange.hybrid import HybridExchangeTransport
from academy.exchange.hybrid import uuid_to_base32
from academy.identifier import ClientId
from academy.message import PingRequest
from academy.socket import open_port
from testing.behavior import EmptyBehavior
from testing.constant import TEST_CONNECTION_TIMEOUT
from testing.constant import TEST_THREAD_JOIN_TIMEOUT


def test_factory_serialize(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    pickled = pickle.dumps(hybrid_exchange_factory)
    reconstructed = pickle.loads(pickled)
    assert isinstance(reconstructed, HybridExchangeFactory)


def test_key_namespaces(mock_redis) -> None:
    namespace = 'foo'
    uid = ClientId.new()
    factory = HybridExchangeFactory(
        redis_host='localhost',
        redis_port=0,
        namespace=namespace,
    )
    with factory._create_transport() as transport:
        assert isinstance(transport, HybridExchangeTransport)

        assert transport._address_key(uid).startswith(f'{namespace}:')
        assert transport._status_key(uid).startswith(f'{namespace}:')
        assert transport._queue_key(uid).startswith(f'{namespace}:')


def test_send_to_mailbox_direct(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    with hybrid_exchange_factory._create_transport() as transport1:
        with hybrid_exchange_factory._create_transport() as transport2:
            message = PingRequest(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
            )
            for _ in range(3):
                transport1.send(transport2.mailbox_id, message)
                assert (
                    transport2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message
                )


def test_send_to_mailbox_indirect(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    messages = 3
    with hybrid_exchange_factory._create_transport() as transport1:
        aid = transport1.register_agent(EmptyBehavior)
        message = PingRequest(src=transport1.mailbox_id, dest=aid)
        for _ in range(messages):
            transport1.send(aid, message)

    with hybrid_exchange_factory._create_transport(mailbox_id=aid) as mailbox:
        for _ in range(messages):
            assert mailbox.recv(timeout=TEST_CONNECTION_TIMEOUT) == message


def test_mailbox_redis_error_logging(
    hybrid_exchange_factory: HybridExchangeFactory,
    caplog,
) -> None:
    caplog.set_level(logging.ERROR)
    with mock.patch(
        'academy.exchange.hybrid.HybridExchangeTransport._pull_messages_from_redis',
        side_effect=RuntimeError('Mock thread error.'),
    ):
        with hybrid_exchange_factory._create_transport() as transport:
            transport._redis_thread.join(TEST_THREAD_JOIN_TIMEOUT)
            assert any(
                f'Error in redis watcher thread for {transport.mailbox_id}'
                in record.message
                for record in caplog.records
                if record.levelname == 'ERROR'
            )


def test_send_to_mailbox_bad_cached_address(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    port1, port2 = open_port(), open_port()
    with hybrid_exchange_factory._create_transport() as transport1:
        aid = transport1.register_agent(EmptyBehavior)

        factory1 = HybridExchangeFactory(
            redis_host='localhost',
            redis_port=0,
            ports=[port1],
        )
        with factory1._create_transport(mailbox_id=aid) as transport2:
            message = PingRequest(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
            )
            transport1.send(transport2.mailbox_id, message)
            assert transport2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message

        # Address of mailbox is now in the exchanges cache but
        # the mailbox is no longer listening on that address.
        address = transport1._address_cache[transport2.mailbox_id]
        socket = transport1._socket_pool._sockets[address]
        socket.close()

        factory2 = HybridExchangeFactory(
            redis_host='localhost',
            redis_port=0,
            ports=[port2],
        )
        with factory2._create_transport(mailbox_id=aid) as transport2:
            # This send will try the cached address, fail, catch the error,
            # and retry via redis.
            transport1.send(transport2.mailbox_id, message)
            assert transport2.recv(timeout=TEST_CONNECTION_TIMEOUT) == message


def test_uuid_encoding() -> None:
    for _ in range(3):
        uid = uuid.uuid4()
        encoded = uuid_to_base32(uid)
        assert base32_to_uuid(encoded) == uid
