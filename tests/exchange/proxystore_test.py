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
from academy.exchange import UnboundExchangeClient
from academy.exchange.cloud.client import UnboundHttpExchangeClient
from academy.exchange.proxystore import UnboundProxyStoreExchange
from academy.exchange.thread import BoundThreadExchangeClient
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import PingRequest
from testing.behavior import EmptyBehavior


@pytest.fixture
def store() -> Generator[Store[LocalConnector], None, None]:
    with Store(
        'proxystore-exchange-store-fixture',
        LocalConnector(),
        cache_size=0,
        register=True,
    ) as store:
        yield store


@pytest.mark.parametrize(
    ('should_proxy', 'resolve_async'),
    (
        (ProxyNever(), False),
        (ProxyAlways(), True),
        (ProxyAlways(), False),
        (lambda x: isinstance(x, str), True),
    ),
)
def test_basic_usage(
    should_proxy: Callable[[Any], bool],
    resolve_async: bool,
    exchange: BoundThreadExchangeClient,
    store: Store[LocalConnector],
) -> None:
    wrapped_exchange_unbound = UnboundProxyStoreExchange(
        exchange.clone(),  # Fixture is already bound, so need to clone
        store,
        should_proxy,
        resolve_async=resolve_async,
    )

    with wrapped_exchange_unbound.bind_as_client() as wrapped_exchange:
        src = wrapped_exchange.mailbox_id
        dest = wrapped_exchange.register_agent(EmptyBehavior)
        status = wrapped_exchange.status(dest)
        assert status == MailboxStatus.ACTIVE

        mailbox = wrapped_exchange_unbound.bind_as_agent(agent_id=dest)
        assert mailbox.mailbox_id == dest

        ping = PingRequest(src=src, dest=dest)
        wrapped_exchange.send(dest, ping)
        assert mailbox.recv() == ping

        request = ActionRequest(
            src=src,
            dest=dest,
            action='test',
            pargs=('value', 123),
            kargs={'foo': 'value', 'bar': 123},
        )
        wrapped_exchange.send(dest, request)

        received = mailbox.recv()
        assert isinstance(received, ActionRequest)
        assert request.tag == received.tag

        for old, new in zip(request.pargs, received.pargs):
            assert (type(new) is Proxy) == should_proxy(old)
            # will resolve the proxy if it exists
            assert old == new

        for name in request.kargs:
            old, new = request.kargs[name], received.kargs[name]
            assert (type(new) is Proxy) == should_proxy(old)
            assert old == new

        response = request.response('result')
        wrapped_exchange.send(dest, response)

        received = mailbox.recv()
        assert isinstance(received, ActionResponse)
        assert response.tag == received.tag
        assert (type(received.result) is Proxy) == should_proxy(
            response.result,
        )
        assert response.result == received.result

        assert wrapped_exchange.discover(EmptyBehavior) == (dest,)

        mailbox.close()
        wrapped_exchange.terminate(src)
        wrapped_exchange.terminate(dest)


def test_serialize(
    http_exchange_server: tuple[str, int],
    store: Store[LocalConnector],
) -> None:
    host, port = http_exchange_server

    unbound_base_exchange = UnboundHttpExchangeClient(host, port)
    unbound_proxystore_exchange = UnboundProxyStoreExchange(
        unbound_base_exchange,
        store,
        should_proxy=ProxyAlways(),
    )
    dumped = pickle.dumps(unbound_proxystore_exchange)
    reconstructed = pickle.loads(dumped)
    assert isinstance(reconstructed, UnboundProxyStoreExchange)

    with unbound_proxystore_exchange.bind_as_client() as exchange:
        dumped = pickle.dumps(exchange)
        reconstructed = pickle.loads(dumped)
        assert isinstance(reconstructed, UnboundProxyStoreExchange)
        assert isinstance(reconstructed.exchange, UnboundHttpExchangeClient)


def test_clone(
    exchange: BoundThreadExchangeClient,
    store: Store[LocalConnector],
) -> None:
    wrapped_exchange_unbound = UnboundProxyStoreExchange(
        exchange.clone(),  # Fixture is already bound, so need to clone
        store,
        ProxyAlways(),
    )

    with wrapped_exchange_unbound.bind_as_client() as wrapped:
        cloned = wrapped.clone()

        assert isinstance(cloned, UnboundProxyStoreExchange)
        assert isinstance(cloned.exchange, UnboundExchangeClient)
