from __future__ import annotations

import logging
import pickle
from unittest import mock

import aiohttp
import pytest

from academy.exception import BadEntityIdError
from academy.exception import ForbiddenError
from academy.exception import MailboxTerminatedError
from academy.exception import UnauthorizedError
from academy.exchange import HttpExchangeFactory
from academy.exchange import HttpExchangeTransport
from academy.exchange.cloud.app import StatusCode
from academy.exchange.cloud.client import _raise_for_status
from academy.exchange.cloud.client import spawn_http_exchange
from academy.identifier import UserId
from academy.socket import open_port
from testing.constant import TEST_CONNECTION_TIMEOUT


def test_factory_serialize(
    http_exchange_factory: HttpExchangeFactory,
) -> None:
    pickled = pickle.dumps(http_exchange_factory)
    reconstructed = pickle.loads(pickled)
    assert isinstance(reconstructed, HttpExchangeFactory)


@pytest.mark.asyncio
async def test_recv_timeout(http_exchange_server: tuple[str, int]) -> None:
    host, port = http_exchange_server
    url = f'http://{host}:{port}'
    factory = HttpExchangeFactory(url)

    class MockResponse:
        status = StatusCode.TIMEOUT.value

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def __aenter__(self):
            return self

    async with await factory._create_transport() as transport:
        response = MockResponse()
        with mock.patch.object(
            transport._session,
            'get',
            return_value=response,
        ):
            with pytest.raises(TimeoutError):
                await transport.recv()


@pytest.mark.asyncio
async def test_additional_headers(
    http_exchange_server: tuple[str, int],
) -> None:
    host, port = http_exchange_server
    url = f'http://{host}:{port}'
    headers = {'Authorization': 'fake auth'}
    factory = HttpExchangeFactory(url, additional_headers=headers)
    async with await factory._create_transport() as transport:
        assert isinstance(transport, HttpExchangeTransport)
        assert 'Authorization' in transport._session.headers


async def test_raise_for_status_error_conversion() -> None:
    class _MockResponse(aiohttp.ClientResponse):
        def __init__(self, status: int) -> None:
            self.status = status

    response = _MockResponse(StatusCode.OKAY.value)
    _raise_for_status(response, UserId.new())

    response = _MockResponse(StatusCode.UNAUTHORIZED.value)
    with pytest.raises(UnauthorizedError):
        _raise_for_status(response, UserId.new())

    response = _MockResponse(StatusCode.FORBIDDEN.value)
    with pytest.raises(ForbiddenError):
        _raise_for_status(response, UserId.new())

    response = _MockResponse(StatusCode.NOT_FOUND.value)
    with pytest.raises(BadEntityIdError):
        _raise_for_status(response, UserId.new())

    response = _MockResponse(StatusCode.TERMINATED.value)
    with pytest.raises(MailboxTerminatedError):
        _raise_for_status(response, UserId.new())

    response = _MockResponse(StatusCode.TIMEOUT.value)
    with pytest.raises(TimeoutError):
        _raise_for_status(response, UserId.new())


@pytest.mark.asyncio
async def test_spawn_http_exchange() -> None:
    with spawn_http_exchange(
        'localhost',
        open_port(),
        level=logging.ERROR,
        timeout=TEST_CONNECTION_TIMEOUT,
    ) as factory:
        async with await factory._create_transport() as transport:
            assert isinstance(transport, HttpExchangeTransport)
