from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock

import pytest

from academy.exception import ForbiddenError
from academy.exception import UnauthorizedError
from academy.exchange.cloud.authenticate import get_authenticator
from academy.exchange.cloud.authenticate import get_token_from_headers
from academy.exchange.cloud.authenticate import GlobusAuthenticator
from academy.exchange.cloud.authenticate import NullAuthenticator
from academy.exchange.cloud.config import ExchangeAuthConfig


@pytest.mark.asyncio
async def test_null_authenticator() -> None:
    user1 = await NullAuthenticator().authenticate_user({})
    user2 = await NullAuthenticator().authenticate_user(
        {'Authorization': 'token'},
    )
    assert user1 == user2


@pytest.mark.asyncio
async def test_authenticate_user_with_token() -> None:
    authenticator = GlobusAuthenticator(str(uuid.uuid4()), '')

    token_meta: dict[str, Any] = {
        'active': True,
        'aud': [authenticator.audience],
        'sub': authenticator.auth_client.client_id,
        'username': 'username',
        'client_id': str(uuid.uuid4()),
        'email': 'username@example.com',
        'name': 'User Name',
    }

    with mock.patch.object(
        authenticator,
        '_token_introspect',
        return_value=token_meta,
    ):
        user = await authenticator.authenticate_user(
            {'Authorization': 'Bearer <TOKEN>'},
        )

    assert user == uuid.UUID(token_meta['client_id'])


@pytest.mark.asyncio
async def test_authenticate_user_with_token_expired_token() -> None:
    authenticator = GlobusAuthenticator(str(uuid.uuid4()), '')
    with (
        mock.patch.object(
            authenticator,
            '_token_introspect',
            return_value={'active': False},
        ),
        pytest.raises(
            ForbiddenError,
            match='Token is expired or has been revoked.',
        ),
    ):
        await authenticator.authenticate_user(
            {'Authorization': 'Bearer <TOKEN>'},
        )


@pytest.mark.asyncio
async def test_authenticate_user_with_token_wrong_audience() -> None:
    authenticator = GlobusAuthenticator(
        str(uuid.uuid4()),
        '',
        audience='audience',
    )
    with (
        mock.patch.object(
            authenticator,
            '_token_introspect',
            return_value={'active': True},
        ),
        pytest.raises(
            ForbiddenError,
            match='Token audience does not include "audience"',
        ),
    ):
        await authenticator.authenticate_user(
            {'Authorization': 'Bearer <TOKEN>'},
        )


@pytest.fixture
def globus_auth_client():
    with mock.patch('globus_sdk.ConfidentialAppAuthClient') as auth_client:
        yield auth_client


def test_globus_authenticator_token_introspect(globus_auth_client: mock.Mock):
    authenticator = GlobusAuthenticator(str(uuid.uuid4()), '')

    authenticator._token_introspect('test')
    globus_auth_client.assert_called_once()

    # Should use thread local client
    authenticator._token_introspect('test2')
    globus_auth_client.assert_called_once()

    with ThreadPoolExecutor() as executor:
        executor.submit(authenticator._token_introspect, 'test3')
        assert globus_auth_client.call_count == 2  # noqa: PLR2004


def test_get_authenticator() -> None:
    config = ExchangeAuthConfig()
    authenticator = get_authenticator(config)
    assert isinstance(authenticator, NullAuthenticator)

    config = ExchangeAuthConfig(
        method='globus',
        kwargs={
            'audience': 'test',
            'client_id': str(uuid.uuid4()),
            'client_secret': 'test',
        },
    )
    authenticator = get_authenticator(config)
    assert isinstance(authenticator, GlobusAuthenticator)
    assert authenticator.audience == 'test'


def test_get_authenticator_unknown() -> None:
    config = ExchangeAuthConfig(method='globus')
    # Modify attribute after construction to avoid Pydantic checking string
    # literal type.
    config.method = 'test'  # type: ignore[assignment]
    with pytest.raises(ValueError, match='Unknown authentication method'):
        get_authenticator(config)


def test_get_token_from_headers() -> None:
    headers = {'Authorization': 'Bearer <TOKEN>'}
    assert get_token_from_headers(headers) == '<TOKEN>'


def test_get_token_from_headers_missing() -> None:
    with pytest.raises(
        UnauthorizedError,
        match='Request headers are missing authorization header.',
    ):
        get_token_from_headers({})


def test_get_token_from_headers_malformed() -> None:
    with pytest.raises(
        UnauthorizedError,
        match='Bearer token in authorization header is malformed.',
    ):
        get_token_from_headers({'Authorization': '<TOKEN>'})
