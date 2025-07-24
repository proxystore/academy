"""Authenticate users from request headers."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol
from typing import runtime_checkable

import globus_sdk
from cachetools import cachedmethod
from cachetools import TTLCache

from academy.exception import ForbiddenError
from academy.exception import UnauthorizedError
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.login import AcademyExchangeScopes


@runtime_checkable
class Authenticator(Protocol):
    """Authenticate users from request headers."""

    async def authenticate_user(self, headers: Mapping[str, str]) -> uuid.UUID:
        """Authenticate user from request headers.

        Warning:
            This method must be thread safe!

        Args:
            headers: Request headers.

        Returns:
            A user id upon authentication success.

        Raises:
            ForbiddenError: user is authenticated but is missing permissions
                or accessing forbidden resources.
            UnauthorizedError: user authentication fails.
        """
        ...


class NullAuthenticator:
    """Authenticator that implements no authentication."""

    async def authenticate_user(self, headers: Mapping[str, str]) -> uuid.UUID:
        """Authenticate user from request headers.

        Args:
            headers: Request headers.

        Returns:
            Null user regardless of provided headers.
        """
        return uuid.UUID(int=0)


class GlobusAuthenticator:
    """Globus Auth authorizer.

    Args:
        client_id: Globus application client ID. If either `client_id`
            or `client_secret` is `None`, the values will be read from the
            environment variables as described in
            [`get_confidential_app_auth_client`][proxystore.globus.client.get_confidential_app_auth_client].
            Ignored if `auth_client` is provided.
        client_secret: Globus application client secret. See `client_id` for
            details. Ignored if `auth_client` is provided.
        audience: Intended audience of the token. This should typically be
            the resource server of the the token was issued for. E.g.,
            the UUID of the ProxyStore Relay Server application.
        token_cache_limit: Maximum number of (token, identity) mappings
            to store in memory.
        token_ttl_s: Time in seconds before invalidating cached tokens.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        audience: str = AcademyExchangeScopes.resource_server,
        token_cache_limit: int = 1024,
        token_ttl_s: int = 60,
    ) -> None:
        self._local_data = threading.local()
        self.executor = ThreadPoolExecutor(
            thread_name_prefix='exchange-auth-thread',
        )
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience
        self.token_cache = TTLCache(
            maxsize=token_cache_limit,
            ttl=token_ttl_s,
        )

    @property
    def auth_client(self) -> globus_sdk.ConfidentialAppAuthClient:
        """A thread local copy of the Globus AuthClient."""
        try:
            return self._local_data.auth_client
        except AttributeError:
            self._local_data.auth_client = (
                globus_sdk.ConfidentialAppAuthClient(
                    client_id=str(self.client_id),
                    client_secret=str(self.client_secret),
                )
            )
            return self._local_data.auth_client

    @cachedmethod(lambda self: self.token_cache)
    def _token_introspect(
        self,
        token: str,
    ) -> globus_sdk.response.GlobusHTTPResponse:
        return self.auth_client.oauth2_token_introspect(token)

    async def authenticate_user(self, headers: Mapping[str, str]) -> uuid.UUID:
        """Authenticate a Globus Auth user from request header.

        This follows from the [Globus Sample Data Portal](https://github.com/globus/globus-sample-data-portal/blob/30e30cd418ee9b103e04916e19deb9902d3aafd8/service/decorators.py)
        example.

        The underlying auth client is not thread safe, but this method is made
        thread safe using a lock.

        Args:
            headers: Request headers to extract tokens from.

        Returns:
            Globus Auth identity returned via \
            [token introspection](https://docs.globus.org/api/auth/reference/#token-introspect).

        Raises:
            UnauthorizedError: if the authorization header is missing or
                the header is malformed.
            ForbiddenError: if the tokens have expired or been revoked.
            ForbiddenError: if `audience` is not included in the token's
                audience.
        """
        token = get_token_from_headers(headers)
        loop = asyncio.get_running_loop()
        token_meta = await loop.run_in_executor(
            self.executor,
            self._token_introspect,
            token,
        )

        if not token_meta.get('active'):
            raise ForbiddenError('Token is expired or has been revoked.')

        if self.audience is not None and self.audience not in token_meta.get(
            'aud',
            [],
        ):
            raise ForbiddenError(
                f'Token audience does not include "{self.audience}". This '
                'could result in a confused deputy attack. Ensure the correct '
                'scopes are requested when the token is created.',
            )

        return uuid.UUID(token_meta.get('client_id'))


def get_authenticator(config: ExchangeAuthConfig) -> Authenticator:
    """Create an authenticator from a configuration.

    Args:
        config: Configuration.

    Returns:
        Authenticator.

    Raises:
        ValueError: if the authentication method in the config is unknown.
    """
    if config.method is None:
        return NullAuthenticator()
    elif config.method == 'globus':
        return GlobusAuthenticator(**config.kwargs)
    else:
        raise ValueError(f'Unknown authentication method "{config.method}."')


def get_token_from_headers(headers: Mapping[str, str]) -> str:
    """Extract token from websockets headers.

    The header is expected to have the format `Authorization: Bearer <TOKEN>`.

    Args:
         headers: Request headers to extract tokens from.

    Returns:
        String token.

    Raises:
        UnauthorizedError: if the authorization header is missing.
        UnauthorizedError: if the authorization header is malformed.
    """
    if 'Authorization' not in headers:
        raise UnauthorizedError(
            'Request headers are missing authorization header.',
        )

    auth_header_parts = headers['Authorization'].split(' ')

    if len(auth_header_parts) != 2 or auth_header_parts[0] != 'Bearer':  # noqa: PLR2004
        raise UnauthorizedError(
            'Bearer token in authorization header is malformed.',
        )

    return auth_header_parts[1]
