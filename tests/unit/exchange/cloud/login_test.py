from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Generator
from unittest import mock

import globus_sdk
import pytest
from globus_sdk.globus_app import ClientApp
from globus_sdk.globus_app import GlobusAppConfig
from globus_sdk.globus_app import UserApp
from globus_sdk.tokenstorage import MemoryTokenStorage
from globus_sdk.tokenstorage import TokenValidationError

from academy.exchange.cloud.login import ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME
from academy.exchange.cloud.login import ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME
from academy.exchange.cloud.login import get_auth_headers
from academy.exchange.cloud.login import get_client_app
from academy.exchange.cloud.login import get_client_credentials_from_env
from academy.exchange.cloud.login import get_globus_app
from academy.exchange.cloud.login import get_token_storage
from academy.exchange.cloud.login import get_user_app
from academy.exchange.cloud.login import is_client_login


@pytest.fixture
def mock_env_credentials() -> Generator[tuple[str, str], None, None]:
    client_uuid = str(uuid.uuid4())
    client_secret = 'secret'

    env = {
        ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME: client_uuid,
        ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME: client_secret,
    }
    with mock.patch.dict(os.environ, env):
        yield client_uuid, client_secret


def test_get_token_storage(tmp_path: pathlib.Path):
    filepath = tmp_path / 'tokens.json'
    get_token_storage(filepath)


def test_get_token_storage_academy_default(tmp_path: pathlib.Path):
    env = {
        'ACADEMY_HOME': str(tmp_path),
    }
    with mock.patch.dict(os.environ, env):
        store = get_token_storage()
        assert store.filepath == str(tmp_path.joinpath('storage.json'))


def test_get_confidential_app_auth_client_from_env(
    mock_env_credentials,
) -> None:
    found_id, found_secret = get_client_credentials_from_env()
    assert found_id == mock_env_credentials[0]
    assert found_secret == mock_env_credentials[1]


@mock.patch('academy.exchange.cloud.login.get_token_storage')
def test_get_globus_app_client_login(
    mock_storage,
    mock_env_credentials,
) -> None:
    mock_storage.return_value = MemoryTokenStorage()
    globus_app = get_globus_app()
    assert isinstance(globus_app, ClientApp)


@mock.patch('academy.exchange.cloud.login.get_token_storage')
def test_get_globus_app_not_client_login(mock_storage) -> None:
    mock_storage.return_value = MemoryTokenStorage()
    globus_app = get_globus_app()
    assert isinstance(globus_app, UserApp)


@mock.patch('academy.exchange.cloud.login.get_token_storage')
def test_get_client_app_from_env(mock_storage, mock_env_credentials) -> None:
    mock_storage.return_value = MemoryTokenStorage()
    globus_app = get_client_app()
    assert isinstance(globus_app, ClientApp)


@mock.patch('academy.exchange.cloud.login.get_token_storage')
def test_get_client_app_custom(mock_storage) -> None:
    mock_storage.return_value = MemoryTokenStorage()
    globus_app = get_client_app(str(uuid.uuid4()), '<secret>')
    assert isinstance(globus_app, ClientApp)


@mock.patch('academy.exchange.cloud.login.get_token_storage')
def test_get_user_app(mock_storage) -> None:
    mock_storage.return_value = MemoryTokenStorage()
    globus_app = get_user_app()
    assert isinstance(globus_app, UserApp)


def test_is_client_login() -> None:
    with mock.patch.dict(os.environ, {}):
        assert not is_client_login()

    env = {ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME: str(uuid.uuid4())}
    with mock.patch.dict(os.environ, env):
        assert not is_client_login()

    env[ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME] = 'secret'
    with mock.patch.dict(os.environ, env):
        assert is_client_login()


def test_get_auth_headers_none() -> None:
    assert get_auth_headers(None) == {}


@pytest.fixture
def globus_app() -> UserApp:
    config = GlobusAppConfig(token_storage=MemoryTokenStorage())
    mock_client = mock.Mock(
        spec=globus_sdk.NativeAppAuthClient,
        client_id='mock-client_id',
        base_url='https://auth.globus.org',
        environment='production',
    )
    return UserApp(
        app_name='test-app',
        login_client=mock_client,
        config=config,
    )


def test_get_auth_headers_globus(globus_app) -> None:
    mock_authorizer = mock.MagicMock()
    header = 'Bearer <TOKEN>'

    with (
        mock.patch(
            'academy.exchange.cloud.login.get_globus_app',
            return_value=globus_app,
        ),
        mock.patch.object(
            globus_app,
            'get_authorizer',
            return_value=mock_authorizer,
        ),
        mock.patch.object(
            mock_authorizer,
            'get_authorization_header',
            return_value=header,
        ),
    ):
        assert get_auth_headers('globus')['Authorization'] == header


def test_get_auth_headers_globus_missing(globus_app) -> None:
    with (
        mock.patch(
            'academy.exchange.cloud.login.get_globus_app',
            return_value=globus_app,
        ),
        mock.patch.object(
            globus_app,
            'get_authorizer',
            side_effect=TokenValidationError(),
        ),
        pytest.raises(
            SystemExit,
        ),
    ):
        assert get_auth_headers('globus')
