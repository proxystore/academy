from __future__ import annotations

import logging
import multiprocessing
import pathlib
import time
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

from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import MailboxStatus
from academy.exchange.cloud.client import HttpExchangeFactory
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.config import ExchangeServingConfig
from academy.exchange.cloud.exceptions import ForbiddenError
from academy.exchange.cloud.login import AcademyExchangeScopes
from academy.exchange.cloud.server import _BAD_REQUEST_CODE
from academy.exchange.cloud.server import _FORBIDDEN_CODE
from academy.exchange.cloud.server import _MailboxManager
from academy.exchange.cloud.server import _main
from academy.exchange.cloud.server import _NOT_FOUND_CODE
from academy.exchange.cloud.server import _OKAY_CODE
from academy.exchange.cloud.server import _run
from academy.exchange.cloud.server import _TIMEOUT_CODE
from academy.exchange.cloud.server import _UNAUTHORIZED_CODE
from academy.exchange.cloud.server import create_app
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.message import PingRequest
from academy.socket import open_port
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

    with mock.patch('academy.exchange.cloud.server._run'):
        assert _main(['--config', str(filepath)]) == 0


def test_server_run() -> None:
    config = ExchangeServingConfig(
        host='127.0.0.1',
        port=open_port(),
        log_level=logging.ERROR,
    )
    context = multiprocessing.get_context('spawn')
    process = context.Process(target=_run, args=(config,))
    process.start()

    while True:
        try:
            client = HttpExchangeFactory(
                config.host,
                config.port,
                scheme='http',
            ).create_user_client()
        except OSError:  # pragma: no cover
            time.sleep(0.01)
        else:
            client.close()
            break

    process.terminate()
    process.join()


@pytest.mark.filterwarnings('ignore:Unverified HTTPS request is being made')
def test_server_run_ssl(ssl_context: SSLContextFixture) -> None:
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

    while True:
        try:
            client = HttpExchangeFactory(
                config.host,
                config.port,
                scheme='https',
                ssl_verify=False,
            ).create_user_client()
        except OSError:  # pragma: no cover
            time.sleep(0.01)
        else:
            client.close()
            break

    process.terminate()
    process.join()


@pytest.mark.asyncio
async def test_mailbox_manager_create_close() -> None:
    manager = _MailboxManager()
    user_id = str(uuid.uuid4())
    uid = ClientId.new()
    # Should do nothing since mailbox doesn't exist
    await manager.terminate(user_id, uid)
    assert manager.check_mailbox(user_id, uid) == MailboxStatus.MISSING
    manager.create_mailbox(user_id, uid)
    assert manager.check_mailbox(user_id, uid) == MailboxStatus.ACTIVE
    manager.create_mailbox(user_id, uid)  # Idempotent check

    bad_user = str(uuid.uuid4())  # Authentication check
    with pytest.raises(ForbiddenError):
        manager.create_mailbox(bad_user, uid)
    with pytest.raises(ForbiddenError):
        manager.check_mailbox(bad_user, uid)
    with pytest.raises(ForbiddenError):
        await manager.terminate(bad_user, uid)

    await manager.terminate(user_id, uid)
    await manager.terminate(user_id, uid)  # Idempotent check


@pytest.mark.asyncio
async def test_mailbox_manager_send_recv() -> None:
    manager = _MailboxManager()
    user_id = str(uuid.uuid4())
    bad_user = str(uuid.uuid4())
    uid = ClientId.new()
    manager.create_mailbox(user_id, uid)

    message = PingRequest(src=uid, dest=uid)
    with pytest.raises(ForbiddenError):
        await manager.put(bad_user, message)
    await manager.put(user_id, message)

    with pytest.raises(ForbiddenError):
        await manager.get(bad_user, uid)
    assert await manager.get(user_id, uid) == message

    await manager.terminate(user_id, uid)


@pytest.mark.asyncio
async def test_mailbox_manager_bad_identifier() -> None:
    manager = _MailboxManager()
    uid = ClientId.new()
    message = PingRequest(src=uid, dest=uid)

    with pytest.raises(BadEntityIdError):
        await manager.get(None, uid)

    with pytest.raises(BadEntityIdError):
        await manager.put(None, message)


@pytest.mark.asyncio
async def test_mailbox_manager_mailbox_closed() -> None:
    manager = _MailboxManager()
    uid = ClientId.new()
    manager.create_mailbox(None, uid)
    await manager.terminate(None, uid)
    message = PingRequest(src=uid, dest=uid)

    with pytest.raises(MailboxClosedError):
        await manager.get(None, uid)

    with pytest.raises(MailboxClosedError):
        await manager.put(None, message)


@pytest_asyncio.fixture
async def cli() -> AsyncGenerator[TestClient[Request, Application]]:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_create_mailbox_validation_error(cli) -> None:
    response = await cli.post('/mailbox', json={'mailbox': 'foo'})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_terminate_validation_error(cli) -> None:
    response = await cli.delete('/mailbox', json={'mailbox': 'foo'})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_discover_validation_error(cli) -> None:
    response = await cli.get('/discover', json={})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid arguments'


@pytest.mark.asyncio
async def test_check_mailbox_validation_error(cli) -> None:
    response = await cli.get('/mailbox', json={'mailbox': 'foo'})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid mailbox ID'


@pytest.mark.asyncio
async def test_send_mailbox_validation_error(cli) -> None:
    response = await cli.put('/message', json={'message': 'foo'})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid message'


@pytest.mark.asyncio
async def test_recv_mailbox_validation_error(cli) -> None:
    response = await cli.get('/message', json={'mailbox': 'foo'})
    assert response.status == _BAD_REQUEST_CODE
    assert await response.text() == 'Missing or invalid mailbox ID'

    response = await cli.get(
        '/message',
        json={'mailbox': ClientId.new().model_dump_json()},
    )
    assert response.status == _NOT_FOUND_CODE
    assert await response.text() == 'Unknown mailbox ID'


@pytest.mark.asyncio
async def test_recv_timeout_error(cli) -> None:
    uid = ClientId.new()
    response = await cli.post(
        '/mailbox',
        json={'mailbox': uid.model_dump_json()},
    )
    assert response.status == _OKAY_CODE

    response = await cli.get(
        '/message',
        json={'mailbox': uid.model_dump_json(), 'timeout': 0.001},
    )
    assert response.status == _TIMEOUT_CODE


@pytest.mark.asyncio
async def test_null_auth_client() -> None:
    auth = ExchangeAuthConfig()
    app = create_app(auth)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/message', json={'mailbox': 'foo'})
        assert response.status == _BAD_REQUEST_CODE
        assert await response.text() == 'Missing or invalid mailbox ID'

        response = await client.get(
            '/message',
            json={'mailbox': ClientId.new().model_dump_json()},
        )
        assert response.status == _NOT_FOUND_CODE
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

    with mock.patch(
        'globus_sdk.ConfidentialAppAuthClient.oauth2_token_introspect',
    ) as mock_token_response:
        mock_token_response.side_effect = authorize
        app = create_app(auth)
        async with TestClient(TestServer(app)) as client:
            yield client


@pytest.mark.asyncio
async def test_globus_auth_client_create_discover_close(auth_client) -> None:
    aid = AgentId.new(name='test').model_dump_json()

    # Create agent
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid, 'behavior': 'foo'},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid, 'behavior': 'foo'},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == _FORBIDDEN_CODE

    # Discover
    response = await auth_client.get(
        '/discover',
        json={'behavior': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_1'},
    )
    response_json = await response.json()
    agent_ids = [
        aid for aid in response_json['agent_ids'].split(',') if len(aid) > 0
    ]
    assert len(agent_ids) == 1
    assert response.status == _OKAY_CODE

    response = await auth_client.get(
        '/discover',
        json={'behavior': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_2'},
    )
    response_json = await response.json()
    agent_ids = [
        aid for aid in response_json['agent_ids'].split(',') if len(aid) > 0
    ]
    assert len(agent_ids) == 0
    assert response.status == _OKAY_CODE

    # Check mailbox
    response = await auth_client.get(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    response = await auth_client.get(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == _FORBIDDEN_CODE

    # Delete mailbox
    response = await auth_client.delete(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == _FORBIDDEN_CODE

    response = await auth_client.delete(
        '/mailbox',
        json={'mailbox': aid},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE


@pytest.mark.asyncio
async def test_globus_auth_client_message(auth_client) -> None:
    aid: AgentId[Any] = AgentId.new(name='test')
    cid = ClientId.new()
    message = PingRequest(src=cid, dest=aid)

    # Create agent
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': aid.model_dump_json(), 'behavior': 'foo'},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    # Create client
    response = await auth_client.post(
        '/mailbox',
        json={'mailbox': cid.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    # Send valid message
    response = await auth_client.put(
        '/message',
        json={'message': message.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    # Send unauthorized message
    response = await auth_client.put(
        '/message',
        json={'message': message.model_dump_json()},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == _FORBIDDEN_CODE

    response = await auth_client.get(
        '/message',
        json={'mailbox': aid.model_dump_json()},
        headers={'Authorization': 'Bearer user_1'},
    )
    assert response.status == _OKAY_CODE

    response = await auth_client.get(
        '/message',
        json={'mailbox': aid.model_dump_json()},
        headers={'Authorization': 'Bearer user_2'},
    )
    assert response.status == _FORBIDDEN_CODE


@pytest.mark.asyncio
async def test_globus_auth_client_missing_auth(auth_client) -> None:
    response = await auth_client.get(
        '/discover',
        json={'behavior': 'foo', 'allow_subclasses': False},
    )
    assert response.status == _UNAUTHORIZED_CODE


@pytest.mark.asyncio
async def test_globus_auth_client_forbidden(auth_client) -> None:
    response = await auth_client.get(
        '/discover',
        json={'behavior': 'foo', 'allow_subclasses': False},
        headers={'Authorization': 'Bearer user_3'},
    )
    assert response.status == _FORBIDDEN_CODE
