from __future__ import annotations

import pickle
from collections.abc import Generator
from typing import Any
from typing import Callable

import pytest
from proxystore.connectors.local import LocalConnector
from proxystore.proxy import Proxy
from proxystore.store import Store
from proxystore.store.executor import ProxyAlways
from proxystore.store.executor import ProxyNever

from academy.exchange import MailboxStatus
from academy.exchange.local import LocalExchangeFactory
from academy.exchange.proxystore import ProxyStoreExchangeFactory
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import Message
from academy.message import PingRequest
from testing.agents import EmptyAgent


@pytest.fixture
def store() -> Generator[Store[LocalConnector], None, None]:
    with Store(
        'proxystore-exchange-store-fixture',
        LocalConnector(),
        cache_size=0,
        register=True,
    ) as store:
        yield store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('should_proxy', 'resolve_async'),
    (
        (ProxyNever(), False),
        (ProxyAlways(), True),
        (ProxyAlways(), False),
        (lambda x: isinstance(x, str), True),
    ),
)
async def test_wrap_basic_transport_functionality(
    should_proxy: Callable[[Any], bool],
    resolve_async: bool,
    store: Store[LocalConnector],
    local_exchange_factory: LocalExchangeFactory,
) -> None:
    wrapped_factory = ProxyStoreExchangeFactory(
        base=local_exchange_factory,
        store=store,
        should_proxy=should_proxy,
        resolve_async=resolve_async,
    )

    async with await wrapped_factory._create_transport() as wrapped_transport1:
        new_factory = wrapped_transport1.factory()
        assert isinstance(new_factory, ProxyStoreExchangeFactory)

        src = wrapped_transport1.mailbox_id
        dest = (await wrapped_transport1.register_agent(EmptyAgent)).agent_id
        assert await wrapped_transport1.status(dest) == MailboxStatus.ACTIVE

        wrapped_transport2 = await wrapped_factory._create_transport(
            mailbox_id=dest,
        )
        assert wrapped_transport2.mailbox_id == dest

        ping = Message.create(src=src, dest=dest, body=PingRequest())
        await wrapped_transport1.send(ping)
        assert await wrapped_transport2.recv() == ping

        sent_request = ActionRequest(
            action='test',
            pargs=('value', 123),
            kargs={'foo': 'value', 'bar': 123},
        )
        sent_request_message = Message.create(
            src=src,
            dest=dest,
            body=sent_request,
        )
        await wrapped_transport1.send(sent_request_message)

        recv_request_message = await wrapped_transport2.recv()
        recv_request = recv_request_message.get_body()
        assert isinstance(recv_request, ActionRequest)
        assert sent_request_message.tag == recv_request_message.tag

        for old, new in zip(sent_request.get_args(), recv_request.get_args()):
            assert (type(new) is Proxy) == should_proxy(old)
            # will resolve the proxy if it exists
            assert old == new

        for name in recv_request.get_kwargs():
            old = sent_request.get_kwargs()[name]
            new = recv_request.get_kwargs()[name]
            assert (type(new) is Proxy) == should_proxy(old)
            assert old == new

        sent_response = ActionResponse(action='test', result='result')
        sent_response_message = sent_request_message.create_response(
            sent_response,
        )
        await wrapped_transport2.send(sent_response_message)

        recv_response_message = await wrapped_transport1.recv()
        recv_response = recv_response_message.get_body()
        assert isinstance(recv_response, ActionResponse)
        assert sent_response_message.tag == recv_response_message.tag
        assert (type(recv_response.get_result()) is Proxy) == should_proxy(
            sent_response.get_result(),
        )
        assert sent_response.get_result() == recv_response.get_result()

        assert await wrapped_transport1.discover(EmptyAgent) == (dest,)

        await wrapped_transport1.terminate(wrapped_transport1.mailbox_id)
        await wrapped_transport2.close()


@pytest.mark.asyncio
async def test_serialize_factory(
    http_exchange_factory,
    store: Store[LocalConnector],
) -> None:
    wrapped_factory = ProxyStoreExchangeFactory(
        base=http_exchange_factory,
        store=store,
        should_proxy=ProxyAlways(),
    )
    dumped = pickle.dumps(wrapped_factory)
    reconstructed = pickle.loads(dumped)
    assert isinstance(reconstructed, ProxyStoreExchangeFactory)
