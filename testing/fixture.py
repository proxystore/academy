from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator

import pytest

from academy.exchange import ExchangeClient
from academy.exchange.cloud.server import create_app
from academy.exchange.cloud.server import serve_app
from academy.exchange.thread import ThreadExchangeFactory
from academy.launcher import ThreadLauncher
from academy.socket import open_port
from testing.constant import TEST_CONNECTION_TIMEOUT
from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.exchange import MailboxStatus
from academy.exchange.cloud.client import HttpExchangeFactory
from academy.exchange.hybrid import HybridExchangeFactory
from academy.exchange.redis import RedisExchangeFactory
from academy.exchange.thread import ThreadExchangeFactory
from academy.identifier import AgentId
from academy.message import PingRequest
from testing.behavior import EmptyBehavior


@pytest.fixture
def http_exchange_factory(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    return HttpExchangeFactory(host, port)


@pytest.fixture
def hybrid_exchange_factory(mock_redis) -> HybridExchangeFactory:
    return HybridExchangeFactory(redis_host='localhost', redis_port=0)


@pytest.fixture
def redis_exchange_factory(mock_redis) -> RedisExchangeFactory:
    return RedisExchangeFactory(hostname='localhost', port=0)


@pytest.fixture
def thread_exchange_factory() -> ThreadExchangeFactory:
    return ThreadExchangeFactory()


@pytest.fixture
def exchange() -> Generator[ExchangeClient]:
    with ThreadExchangeFactory().bind_as_client(
        start_listener=False,
    ) as exchange:
        yield exchange


@pytest.fixture
def launcher() -> Generator[ThreadLauncher]:
    with ThreadLauncher() as launcher:
        yield launcher


@pytest.fixture
def http_exchange_server() -> Generator[tuple[str, int]]:
    host, port = 'localhost', open_port()
    app = create_app()
    loop = asyncio.new_event_loop()
    started = threading.Event()
    stop = loop.create_future()

    async def _run() -> None:
        async with serve_app(app, host, port):
            started.set()
            await stop

    def _target() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run())
        loop.close()

    handle = threading.Thread(target=_target, name='http-exchange-fixture')
    handle.start()

    started.wait(TEST_CONNECTION_TIMEOUT)

    yield host, port

    loop.call_soon_threadsafe(stop.set_result, None)
    handle.join(timeout=TEST_CONNECTION_TIMEOUT)
    if handle.is_alive():  # pragma: no cover
        raise TimeoutError(
            'Server thread did not gracefully exit within '
            f'{TEST_CONNECTION_TIMEOUT} seconds.',
        )
