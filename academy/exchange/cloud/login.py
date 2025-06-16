"""Create [`GlobusApp`][globus_sdk.GlobusApp] instances.

Taken from: https://github.com/proxystore/proxystore/blob/e296f937d913cae02f87ab35112fd4c8351860b0/proxystore/globus/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Any
from typing import Literal

import click
from globus_sdk.globus_app import ClientApp
from globus_sdk.globus_app import GlobusApp
from globus_sdk.globus_app import GlobusAppConfig
from globus_sdk.globus_app import UserApp
from globus_sdk.login_flows import CommandLineLoginFlowManager
from globus_sdk.scopes import ScopeBuilder
from globus_sdk.tokenstorage import SQLiteTokenStorage
from globus_sdk.tokenstorage import TokenValidationError

# Registered `Academy-Client Application` by alokvk2@uchicago.edu
ACADEMY_GLOBUS_CLIENT_ID = '1624cf3f-45ee-4f54-9de4-2d5d79191346'

ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME = 'ACADEMY_GLOBUS_CLIENT_ID'
ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME = 'ACADEMY_GLOBUS_CLIENT_SECRET'

_APP_NAME = 'academy'
_TOKENS_FILE = 'storage.db'

AcademyExchangeScopes = ScopeBuilder(
    # "Academy Exchange Server" application client ID
    'a7e16357-8edf-414d-9e73-85e4b0b18be4',
    # The academy_exchange scope has scope ID:
    #   17619205-054c-4829-a1a8-f4b6968c76d2
    known_url_scopes=['academy_exchange'],
)


class _CustomLoginFlowManager(CommandLineLoginFlowManager):
    def print_authorize_url(
        self,
        authorize_url: str,
    ) -> None:  # pragma: no cover
        click.secho(
            'Please visit the following url to authenticate:',
            fg='cyan',
        )
        click.echo(authorize_url)

    def prompt_for_code(self) -> str:  # pragma: no cover
        auth_code = click.prompt(
            click.style('Enter the auth code:', fg='cyan'),
            prompt_suffix=' ',
        )
        return auth_code.strip()


def get_token_storage(
    filepath: str | pathlib.Path | None = None,
    *,
    namespace: str = 'DEFAULT',
) -> SQLiteTokenStorage:
    """Create token storage adapter.

    Args:
        filepath: Name of the database file. If not provided, defaults to a
            file in the Academy home directory specified by ACADEMY_HOME.
        namespace: Optional namespace to use within the database for
            partitioning token data.

    Returns:
        Token storage.
    """
    if filepath is None:
        default = os.path.join(
            os.path.expanduser('~/local/share'),
            _APP_NAME,
        )
        basepath = os.environ.get('ACADEMY_HOME', default=default)
        filepath = os.path.join(basepath, _TOKENS_FILE)

    filepath = pathlib.Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteTokenStorage(filepath, namespace=namespace)


def get_client_credentials_from_env() -> tuple[str, str]:
    """Read the Globus Client ID and secret from the environment.

    The Client ID should be set to `ACADEMY_GLOBUS_CLIENT_ID` and
    the secret to `ACADEMY_GLOBUS_CLIENT_SECRET`.

    Note:
        This function performs no validation on the values of the variables.

    Returns:
        Tuple containing the client ID and secret.

    Raises:
        ValueError: if one of the environment variables is set.
    """
    try:
        client_id = os.environ[ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME]
        client_secret = os.environ[ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME]
    except KeyError as e:
        raise ValueError(
            f'Both {ACADEMY_GLOBUS_CLIENT_ID_ENV_NAME} and '
            f'{ACADEMY_GLOBUS_CLIENT_SECRET_ENV_NAME} must be set to '
            'use a client identity. Either set both environment variables '
            'or unset both to use the normal login flow.',
        ) from e

    return client_id, client_secret


def get_globus_app() -> GlobusApp:
    """Get a Globus App based on the environment.

    If a client ID and secret are set in the environment, returns a
    [`ClientApp`][globus_sdk.ClientApp] using
    [`get_client_app()`][academy.exchange.cloud.login.get_client_app].
    Otherwise returns a [`UserApp`][globus_sdk.UserApp] using
    [`get_user_app()`][academy.exchange.cloud.login.get_user_app].

    Returns:
        Initialized app.
    """
    if is_client_login():
        return get_client_app()
    return get_user_app()


def get_client_app(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> ClientApp:
    """Get a Client Globus App.

    Args:
        client_id: Client ID. If one or both of the `client_id` and
            `client_secret` are not provided, the values will be read from
            the environment using
            [`get_client_credentials_from_env()`][academy.exchange.cloud.login.get_client_credentials_from_env].
        client_secret: Client secret. See above.

    Returns:
        Initialized app.
    """
    if client_id is None or client_secret is None:
        client_id, client_secret = get_client_credentials_from_env()

    config = GlobusAppConfig(
        token_storage=get_token_storage(),
        request_refresh_tokens=True,
    )

    return ClientApp(
        app_name=_APP_NAME,
        client_id=client_id,
        client_secret=client_secret,
        config=config,
    )


def get_user_app() -> UserApp:
    """Get a User Globus App.

    The [`UserApp`][globus_sdk.UserApp] will
    automatically perform an interactive flow with the user as needed.

    Returns:
        Initialized app.
    """
    config = GlobusAppConfig(
        login_flow_manager=_CustomLoginFlowManager,
        token_storage=get_token_storage(),
        request_refresh_tokens=True,
    )

    return UserApp(
        app_name=_APP_NAME,
        client_id=ACADEMY_GLOBUS_CLIENT_ID,
        config=config,
    )


def is_client_login() -> bool:
    """Check if Globus client identity environment variables are set.

    Based on the Globus Compute SDK's
    [`is_client_login()`](https://github.com/funcx-faas/funcX/blob/8f5b59075ae6f8e8b8b13fe1b91430271f4e0c3c/compute_sdk/globus_compute_sdk/sdk/login_manager/client_login.py#L24-L38){target=_blank}.

    Returns:
        `True` if `ACADEMY_GLOBUS_CLIENT_ID` and \
        `ACADEMY_GLOBUS_CLIENT_SECRET` are set.
    """
    try:
        get_client_credentials_from_env()
    except ValueError:
        return False
    else:
        return True


def get_auth_headers(
    method: Literal['globus'] | None,
    **kwargs: Any,
) -> dict[str, str]:
    """Client utility method to perform authentication and get headers."""
    if method is None:
        return {}
    elif method == 'globus':
        app = get_globus_app()
        app.add_scope_requirements(
            {
                AcademyExchangeScopes.resource_server: [
                    AcademyExchangeScopes.academy_exchange,
                ],
            },
        )
        try:
            authorizer = app.get_authorizer(
                AcademyExchangeScopes.resource_server,
            )
        except TokenValidationError:
            raise SystemExit(1) from None

        bearer = authorizer.get_authorization_header()
        assert bearer is not None
        return {'Authorization': bearer}
    else:
        raise AssertionError('Unreachable.')
