from __future__ import annotations

import logging

from academy.exchange.cloud.client import HttpExchangeFactory
from academy.exchange.cloud.client import HttpExchangeTransport
from academy.exchange.cloud.client import spawn_http_exchange
from academy.socket import open_port
from testing.constant import TEST_CONNECTION_TIMEOUT


def test_additional_headers(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    headers = {'Authorization': 'fake auth'}
    factory = HttpExchangeFactory(host, port, headers)
    with factory._create_transport() as transport:
        assert isinstance(transport, HttpExchangeTransport)
        assert 'Authorization' in transport._session.headers


def test_spawn_http_exchange() -> None:
    with spawn_http_exchange(
        'localhost',
        open_port(),
        level=logging.ERROR,
        timeout=TEST_CONNECTION_TIMEOUT,
    ) as factory:
        with factory._create_transport() as transport:
            assert isinstance(transport, HttpExchangeTransport)
