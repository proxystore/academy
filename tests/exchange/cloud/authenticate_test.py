from __future__ import annotations

import uuid
from typing import Any
from unittest import mock

import pytest

from academy.exchange.cloud.authenticate import get_authenticator
from academy.exchange.cloud.authenticate import get_token_from_headers
from academy.exchange.cloud.authenticate import GlobusAuthenticator
from academy.exchange.cloud.authenticate import NullAuthenticator
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.exceptions import ForbiddenError
from academy.exchange.cloud.exceptions import UnauthorizedError


def test_null_authenticator() -> None:
    user1 = NullAuthenticator().authenticate_user({})
    user2 = NullAuthenticator().authenticate_user({'Authorization': 'token'})
    assert user1 == user2


def test_authenticate_user_with_token() -> None:
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
        authenticator.auth_client,
        'oauth2_token_introspect',
        return_value=token_meta,
    ):
        user = authenticator.authenticate_user(
            {'Authorization': 'Bearer <TOKEN>'},
        )

    assert user == uuid.UUID(token_meta['client_id'])


def test_authenticate_user_with_token_expired_token() -> None:
    authenticator = GlobusAuthenticator(str(uuid.uuid4()), '')
    with (
        mock.patch.object(
            authenticator.auth_client,
            'oauth2_token_introspect',
            return_value={'active': False},
        ),
        pytest.raises(
            ForbiddenError,
            match='Token is expired or has been revoked.',
        ),
    ):
        authenticator.authenticate_user({'Authorization': 'Bearer <TOKEN>'})


def test_authenticate_user_with_token_wrong_audience() -> None:
    authenticator = GlobusAuthenticator(
        str(uuid.uuid4()),
        '',
        audience='audience',
    )
    with (
        mock.patch.object(
            authenticator.auth_client,
            'oauth2_token_introspect',
            return_value={'active': True},
        ),
        pytest.raises(
            ForbiddenError,
            match='Token audience does not include "audience"',
        ),
    ):
        authenticator.authenticate_user({'Authorization': 'Bearer <TOKEN>'})


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
