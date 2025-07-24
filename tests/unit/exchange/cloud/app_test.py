from __future__ import annotations

import logging
import multiprocessing
import pathlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from aiohttp.test_utils import TestServer
from aiohttp.web import Application
from aiohttp.web import Request

from academy.exchange import HttpExchangeFactory
from academy.exchange.cloud.app import _main
from academy.exchange.cloud.app import _run
from academy.exchange.cloud.app import create_app
from academy.exchange.cloud.app import StatusCode
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.config import ExchangeServingConfig
from academy.exchange.cloud.config import PythonBackendConfig
from academy.exchange.cloud.login import AcademyExchangeScopes
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.message import Message
from academy.message import PingRequest
from academy.socket import open_port
from academy.socket import wait_connection
from testing.constant import TEST_CONNECTION_TIMEOUT
from testing.constant import TEST_SLEEP_INTERVAL
from testing.ssl import SSLContextFixture


def test_server_cli(tmp_path: pathlib.Path) -> None:
    data = """\
host = "localhost"
port = 1234
certfile = "/path/to/cert.pem"
keyfile = "/path/to/privkey.pem"

[auth]
method = "globus"

[auth.kwargs]
client_id = "ABC"
"""

    filepath = tmp_path / 'exchange.toml'
    with open(filepath, 'w') as f:
        f.write(data)

    with mock.patch('academy.exchange.cloud.app._run'):
        assert _main(['--config', str(filepath)]) == 0


@pytest.mark.asyncio
async def test_server_run() -> None:
    config = ExchangeServingConfig(
        host='127.0.0.1',
        port=open_port(),
        log_level=logging.ERROR,
    )

    context = multiprocessing.get_context('spawn')
    process = context.Process(target=_run, args=(config,))
    process.start()

    wait_connection(config.host, config.port, timeout=TEST_CONNECTION_TIMEOUT)
    factory = HttpExchangeFactory(f'http://{config.host}:{config.port}')
    client = await factory.create_user_client()
    await client.close()

    process.terminate()
    process.join()
    assert process.exitcode == 0


@pytest.mark.filterwarnings('ignore:Unverified HTTPS request is being made')
@pytest.mark.asyncio
async def test_server_run_ssl(ssl_context: SSLContextFixture) -> None:
    config = ExchangeServingConfig(
        host='127.0.0.1',
        port=open_port(),
        log_level=logging.ERROR,
    )
    config.certfile = ssl_context.certfile
    config.keyfile = ssl_context.keyfile

    context = multiprocessing.get_context('spawn')
    process = context.Process(target=_run, args=(config,))
    process.start()

    wait_connection(config.host, config.port, timeout=TEST_CONNECTION_TIMEOUT)
    factory = HttpExchangeFactory(
        f'https://{config.host}:{config.port}',
        ssl_verify=False,
    )
    client = await factory.create_user_client()
    await client.close()

    process.terminate()
    process.join()
    assert process.exitcode == 0


@pytest_asyncio.fixture
async def cli() -> AsyncGenerator[TestClient[Request, Application]]:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_create_mailbox_validation_error(cli) -> None:
    response = await cli.post('/mailbox', json={'mailbox': 'foo'})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_terminate_validation_error(cli) -> None:
    response = await cli.delete('/mailbox', json={'mailbox': 'foo'})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_discover_validation_error(cli) -> None:
    response = await cli.get('/discover', json={})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid arguments'


@pytest.mark.asyncio
async def test_check_mailbox_validation_error(cli) -> None:
    response = await cli.get('/mailbox', json={'mailbox': 'foo'})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_send_mailbox_validation_error(cli) -> None:
    response = await cli.put('/message', json={'message': 'foo'})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid message'


@pytest.mark.asyncio
async def test_recv_mailbox_validation_error(cli) -> None:
    response = await cli.get('/message', json={'mailbox': 'foo'})
    assert response.status == StatusCode.BAD_REQUEST.value
    assert await response.text() == 'Missing or invalid mailbox ID'

    response = await cli.get(
        '/message',
        json={'mailbox': UserId.new().model_dump_json()},
    )
    assert response.status == StatusCode.NOT_FOUND.value
    assert await response.text() == 'Unknown mailbox ID'


@pytest.mark.asyncio
async def test_recv_timeout_error(cli) -> None:
    uid = UserId.new()
    response = await cli.post(
        '/mailbox',
        json={'mailbox': uid.model_dump_json()},
    )
    assert response.status == StatusCode.OKAY.value

    response = await cli.get(
        '/message',
        json={
            'mailbox': uid.model_dump_json(),
            'timeout': TEST_SLEEP_INTERVAL,
        },
    )
    assert response.status == StatusCode.TIMEOUT.value


@pytest.mark.asyncio
async def test_send_mailbox_message_too_large(cli) -> None:
    aid: AgentId[Any] = AgentId.new()
    cid = UserId.new()
    message = Message.create(src=cid, dest=aid, body=PingRequest())

    with mock.patch('sys.getsizeof', return_value=5 * 1024 * 1024):
        # Create agent
        response = await cli.post(
            '/mailbox',
            json={'mailbox': aid.model_dump_json(), 'agent': 'foo'},
            headers={'Authorization': 'Bearer user_1'},
        )
        assert response.status == StatusCode.OKAY.value

        # Create client
        response = await cli.post(
            '/mailbox',
            json={'mailbox': cid.model_dump_json()},
            headers={'Authorization': 'Bearer user_1'},
        )
        assert response.status == StatusCode.OKAY.value

        # Send valid message
        response = await cli.put(
            '/message',
            json={'message': message.model_dump_json()},
            headers={'Authorization': 'Bearer user_1'},
        )
        assert response.status == StatusCode.TOO_LARGE.value


@pytest.mark.asyncio
async def test_null_auth_client() -> None:
    auth = ExchangeAuthConfig()
    backend = PythonBackendConfig()
    app = create_app(backend, auth)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/message', json={'mailbox': 'foo'})
        assert response.status == StatusCode.BAD_REQUEST.value
        assert await response.text() == 'Missing or invalid mailbox ID'

        response = await client.get(
            '/message',
            json={'mailbox': UserId.new().model_dump_json()},
        )
        assert response.status == StatusCode.NOT_FOUND.value
        assert await response.text() == 'Unknown mailbox ID'


@pytest_asyncio.fixture
async def auth_client() -> AsyncGenerator[TestClient[Request, Application]]:
    auth = ExchangeAuthConfig(
        method='globus',
        kwargs={'client_id': str(uuid.uuid4()), 'client_secret': ''},
    )
    user_1: dict[str, Any] = {
        'active': True,
        'username': 'username',
        'client_id': str(uuid.uuid4()),
        'email': 'username@example.com',
        'name': 'User Name',
        'aud': [AcademyExchangeScopes.resource_server],
    }

    user_2: dict[str, Any] = {
        'active': True,
        'username': 'username',
        'client_id': str(uuid.uuid4()),
        'email': 'username@example.com',
        'name': 'User Name',
        'aud': [AcademyExchangeScopes.resource_server],
    }

    inactive: dict[str, Any] = {
        'active': False,
    }

    def authorize(token):
        if token == 'user_1':
            return user_1
        if token == 'user_2':
            return user_2
        else:
            return inactive

    backend = PythonBackendConfig()

    with mock.patch(
        'globus_sdk.ConfidentialAppAuthClient.oauth2_token_introspect',
    ) as mock_token_response:
        mock_token_response.side_effect = authorize
        app = create_app(backend, auth)
        async with TestClient(TestServer(app)) as client:
            yield client


@pytest.mark.asyncio
async def test_globus_auth_client_create_discover_close(auth_client) -> None:
    aid = AgentId.new(name='test').model_dump_json()

    # Create agent
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid, 'agent': 'foo'},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid, 'agent': 'foo'},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == StatusCode.FORBIDDEN.value

    # Discover
    response = await auth_client.get(
        '/discover',
        json={'agent': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_1'},
    )
    response_json = await response.json()
    agent_ids = [
        aid for aid in response_json['agent_ids'].split(',') if len(aid) > 0
    ]
    assert len(agent_ids) == 1
    assert response.status == StatusCode.OKAY.value

    response = await auth_client.get(
        '/discover',
        json={'agent': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_2'},
    )
    response_json = await response.json()
    agent_ids = [
        aid for aid in response_json['agent_ids'].split(',') if len(aid) > 0
    ]
    assert len(agent_ids) == 0
    assert response.status == StatusCode.OKAY.value

    # Check mailbox
    response = await auth_client.get(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    response = await auth_client.get(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == StatusCode.FORBIDDEN.value

    # Delete mailbox
    response = await auth_client.delete(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == StatusCode.FORBIDDEN.value

    response = await auth_client.delete(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value


@pytest.mark.asyncio
async def test_globus_auth_client_message(auth_client) -> None:
    aid: AgentId[Any] = AgentId.new(name='test')
    cid = UserId.new()
    message = Message.create(src=cid, dest=aid, body=PingRequest())

    # Create agent
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid.model_dump_json(), 'agent': 'foo'},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    # Create client
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': cid.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    # Send valid message
    response = await auth_client.put(
        '/message',
        json={'message': message.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    # Send unauthorized message
    response = await auth_client.put(
        '/message',
        json={'message': message.model_dump_json()},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == StatusCode.FORBIDDEN.value

    response = await auth_client.get(
        '/message',
        json={'mailbox': aid.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == StatusCode.OKAY.value

    response = await auth_client.get(
        '/message',
        json={'mailbox': aid.model_dump_json()},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == StatusCode.FORBIDDEN.value


@pytest.mark.asyncio
async def test_globus_auth_client_missing_auth(auth_client) -> None:
    response = await auth_client.get(
        '/discover',
        json={'agent': 'foo', 'allow_subclasses': False},
    )
    assert response.status == StatusCode.UNAUTHORIZED.value


@pytest.mark.asyncio
async def test_globus_auth_client_forbidden(auth_client) -> None:
    response = await auth_client.get(
        '/discover',
        json={'agent': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_3'},
    )
    assert response.status == StatusCode.FORBIDDEN.value
