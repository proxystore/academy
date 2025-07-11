from __future__ import annotations

import logging
import pickle
import uuid
from unittest import mock

import pytest

from academy.exchange.hybrid import base32_to_uuid
from academy.exchange.hybrid import HybridExchangeFactory
from academy.exchange.hybrid import HybridExchangeTransport
from academy.exchange.hybrid import uuid_to_base32
from academy.identifier import UserId
from academy.message import Message
from academy.message import PingRequest
from academy.socket import open_port
from testing.agents import EmptyAgent
from testing.constant import TEST_CONNECTION_TIMEOUT


@pytest.mark.asyncio
async def test_factory_serialize(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    pickled = pickle.dumps(hybrid_exchange_factory)
    reconstructed = pickle.loads(pickled)
    assert isinstance(reconstructed, HybridExchangeFactory)


@pytest.mark.asyncio
async def test_key_namespaces(mock_redis) -> None:
    namespace = 'foo'
    uid = UserId.new()
    factory = HybridExchangeFactory(
        redis_host='localhost',
        redis_port=0,
        namespace=namespace,
    )
    async with await factory._create_transport() as transport:
        assert isinstance(transport, HybridExchangeTransport)

        assert transport._address_key(uid).startswith(f'{namespace}:')
        assert transport._status_key(uid).startswith(f'{namespace}:')
        assert transport._queue_key(uid).startswith(f'{namespace}:')


@pytest.mark.asyncio
async def test_send_to_mailbox_direct(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    factory = hybrid_exchange_factory
    async with await factory._create_transport() as transport1:
        async with await factory._create_transport() as transport2:
            message = Message.create(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
                body=PingRequest(),
            )
            for _ in range(3):
                await transport1.send(message)
                response = await transport2.recv(
                    timeout=TEST_CONNECTION_TIMEOUT,
                )
                assert response == message


@pytest.mark.asyncio
async def test_send_to_mailbox_indirect(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    factory = hybrid_exchange_factory
    messages = 3
    async with await factory._create_transport() as transport1:
        aid = (await transport1.register_agent(EmptyAgent)).agent_id
        message = Message.create(
            src=transport1.mailbox_id,
            dest=aid,
            body=PingRequest(),
        )
        for _ in range(messages):
            await transport1.send(message)

    async with await factory._create_transport(mailbox_id=aid) as mailbox:
        for _ in range(messages):
            received = await mailbox.recv(timeout=TEST_CONNECTION_TIMEOUT)
            assert received == message


@pytest.mark.asyncio
async def test_mailbox_redis_error_logging(
    hybrid_exchange_factory: HybridExchangeFactory,
    caplog,
) -> None:
    factory = hybrid_exchange_factory
    caplog.set_level(logging.ERROR)
    with mock.patch(
        'academy.exchange.hybrid.HybridExchangeTransport._get_message_from_redis',
        side_effect=RuntimeError('Mock thread error.'),
    ):
        async with await factory._create_transport() as transport:
            assert any(
                f'Error in redis listener task for {transport.mailbox_id}'
                in record.message
                for record in caplog.records
                if record.levelname == 'ERROR'
            )


@pytest.mark.asyncio
async def test_send_to_mailbox_bad_cached_address(
    hybrid_exchange_factory: HybridExchangeFactory,
) -> None:
    factory = hybrid_exchange_factory
    port1, port2 = open_port(), open_port()
    async with await factory._create_transport() as transport1:
        aid = (await transport1.register_agent(EmptyAgent)).agent_id

        factory1 = HybridExchangeFactory(
            redis_host='localhost',
            redis_port=0,
            ports=[port1],
        )
        async with await factory1._create_transport(
            mailbox_id=aid,
        ) as transport2:
            message = Message.create(
                src=transport1.mailbox_id,
                dest=transport2.mailbox_id,
                body=PingRequest(),
            )
            await transport1.send(message)
            received = await transport2.recv(timeout=TEST_CONNECTION_TIMEOUT)
            assert received == message

        # Address of mailbox is now in the exchanges cache but
        # the mailbox is no longer listening on that address.
        address = transport1._address_cache[transport2.mailbox_id]
        socket = transport1._socket_pool._sockets[address]
        await socket.close()

        factory2 = HybridExchangeFactory(
            redis_host='localhost',
            redis_port=0,
            ports=[port2],
        )
        async with await factory2._create_transport(
            mailbox_id=aid,
        ) as transport2:
            # This send will try the cached address, fail, catch the error,
            # and retry via redis.
            await transport1.send(message)
            received = await transport2.recv(timeout=TEST_CONNECTION_TIMEOUT)
            assert received == message


def test_uuid_encoding() -> None:
    for _ in range(3):
        uid = uuid.uuid4()
        encoded = uuid_to_base32(uid)
        assert base32_to_uuid(encoded) == uid
