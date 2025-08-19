from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from typing import Callable

import pytest
import pytest_asyncio
import responses
from aiohttp.web import AppRunner
from aiohttp.web import TCPSite
from globus_sdk._testing import load_response
from globus_sdk._testing import load_response_set

from academy.exchange import ExchangeFactory
from academy.exchange import HttpExchangeFactory
from academy.exchange import HybridExchangeFactory
from academy.exchange import LocalExchangeFactory
from academy.exchange import LocalExchangeTransport
from academy.exchange import RedisExchangeFactory
from academy.exchange import UserExchangeClient
from academy.exchange.cloud.app import create_app
from academy.exchange.cloud.globus import AcademyGlobusClient
from academy.exchange.cloud.globus import GlobusExchangeFactory
from academy.exchange.cloud.login import ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME
from academy.exchange.cloud.login import ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME
from academy.manager import Manager
from academy.socket import open_port
from testing.constant import TEST_PROJECT_ID


@pytest_asyncio.fixture
async def http_exchange_factory(
    http_exchange_server: tuple[str, int],
) -> HttpExchangeFactory:
    host, port = http_exchange_server
    url = f'http://{host}:{port}'
    return HttpExchangeFactory(url)


@pytest.fixture
def hybrid_exchange_factory(mock_redis) -> HybridExchangeFactory:
    return HybridExchangeFactory(redis_host='localhost', redis_port=0)


@pytest.fixture
def redis_exchange_factory(mock_redis) -> RedisExchangeFactory:
    return RedisExchangeFactory(hostname='localhost', port=0)


@pytest.fixture
def local_exchange_factory() -> LocalExchangeFactory:
    return LocalExchangeFactory()


EXCHANGE_FACTORY_TYPES = (
    HttpExchangeFactory,
    HybridExchangeFactory,
    RedisExchangeFactory,
    LocalExchangeFactory,
    GlobusExchangeFactory,
)


@pytest.fixture
async def activate_responses(monkeypatch) -> AsyncGenerator[None]:
    responses.start()
    monkeypatch.setitem(os.environ, 'GLOBUS_SDK_ENVIRONMENT', 'production')
    yield
    responses.stop()
    responses.reset()


@pytest_asyncio.fixture
async def get_factory(
    monkeypatch,
    http_exchange_server: tuple[str, int],
    mock_redis,
    activate_responses,
) -> Callable[[type[ExchangeFactory[Any]]], ExchangeFactory[Any]]:
    # Typically we would parameterize fixtures on a list of the
    # factory fixtures defined above. However, request.getfixturevalue does
    # not work with async fixtures, of which we have a mix, so we need to set
    # them up manually. Instead, we have a fixture that returns a function
    # that can create the factories from a parameterized list of factory types.
    # See: https://github.com/pytest-dev/pytest-asyncio/issues/976
    def _get_factory_for_testing(
        factory_type: type[ExchangeFactory[Any]],
    ) -> ExchangeFactory[Any]:
        if factory_type is HttpExchangeFactory:
            host, port = http_exchange_server
            url = f'http://{host}:{port}'
            return HttpExchangeFactory(url)
        elif factory_type is HybridExchangeFactory:
            return HybridExchangeFactory(redis_host='localhost', redis_port=0)
        elif factory_type is RedisExchangeFactory:
            return RedisExchangeFactory(hostname='localhost', port=0)
        elif factory_type is LocalExchangeFactory:
            return LocalExchangeFactory()
        elif factory_type is GlobusExchangeFactory:
            host, port = http_exchange_server
            base_url = f'http://{host}:{port}'
            responses.add_passthru(base_url)

            monkeypatch.setattr(AcademyGlobusClient, 'base_url', base_url)
            monkeypatch.setitem(
                os.environ,
                ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME,
                'dummy_client_id',
            )
            monkeypatch.setitem(
                os.environ,
                ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME,
                'dummy_client_secret',
            )

            load_response(
                'auth.create_client',
                case='client_type_hybrid_confidential_client_resource_server',
            )
            load_response('auth.create_client_credentials')
            load_response('auth.create_scope')
            load_response('auth.oauth2_get_dependent_tokens')
            load_response('auth.delete_client')
            load_response_set('auth.oauth2_client_credentials_tokens')

            return GlobusExchangeFactory(uuid.UUID(TEST_PROJECT_ID))
        else:
            raise AssertionError('Unsupported factory type.')

    return _get_factory_for_testing


@pytest_asyncio.fixture
async def exchange_client() -> AsyncGenerator[
    UserExchangeClient[LocalExchangeTransport]
]:
    factory = LocalExchangeFactory()
    async with await factory.create_user_client() as client:
        yield client


@pytest.fixture
async def manager(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> AsyncGenerator[Manager[LocalExchangeTransport]]:
    async with Manager(exchange_client, ThreadPoolExecutor()) as manager:
        yield manager


@pytest_asyncio.fixture
async def http_exchange_server() -> AsyncGenerator[tuple[str, int]]:
    host, port = 'localhost', open_port()
    app = create_app()

    runner = AppRunner(app)
    await runner.setup()

    try:
        site = TCPSite(runner, host, port)
        await site.start()
        yield host, port
    finally:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def set_temp_token_storage(monkeypatch, tmp_path: pathlib.Path):
    """Temporary token storage after each test.

    For repeatability, especially in CI pipeline.
    """
    monkeypatch.setitem(os.environ, 'ACADEMY_HOME', str(tmp_path))
