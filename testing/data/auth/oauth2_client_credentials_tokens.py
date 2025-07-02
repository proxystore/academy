from __future__ import annotations

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set
from responses import matchers

from testing.data.auth._common import CLIENT_ID

_token = 'DUMMY_TRANSFER_TOKEN_FROM_THE_INTERTUBES'
_scope = 'https://auth.globus.org/scopes/a7e16357-8edf-414d-9e73-85e4b0b18be4/academy_exchange'

RESPONSES = ResponseSet(
    default=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        status=200,
        json={
            'access_token': _token,
            'scope': _scope,
            'expires_in': 172800,
            'token_type': 'Bearer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'other_tokens': [],
        },
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'grant_type': 'client_credentials',
                    'scope': 'openid openid profile email',
                },
            ),
        ],
        metadata={
            'service': 'transfer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'access_token': _token,
            'scope': _scope,
        },
    ),
    agent=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        status=200,
        json={
            'access_token': _token,
            'scope': _scope,
            'expires_in': 172800,
            'token_type': 'Bearer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'other_tokens': [],
        },
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'grant_type': 'client_credentials',
                    'scope': f'https://auth.globus.org/scopes/{CLIENT_ID}/launch',
                },
            ),
        ],
        metadata={
            'service': 'transfer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'access_token': _token,
            'scope': _scope,
        },
    ),
    agent_2=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        status=200,
        json={
            'access_token': _token,
            'scope': _scope,
            'expires_in': 172800,
            'token_type': 'Bearer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'other_tokens': [],
        },
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'grant_type': 'client_credentials',
                    'scope': f'https://auth.globus.org/scopes/{CLIENT_ID}/launch '  # noqa 501
                    f'https://auth.globus.org/scopes/{CLIENT_ID}/launch',
                },
            ),
        ],
        metadata={
            'service': 'transfer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'access_token': _token,
            'scope': _scope,
        },
    ),
    agent_3=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        status=200,
        json={
            'access_token': _token,
            'scope': _scope,
            'expires_in': 172800,
            'token_type': 'Bearer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'other_tokens': [],
        },
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'grant_type': 'client_credentials',
                    'scope': f'https://auth.globus.org/scopes/{CLIENT_ID}/launch '  # noqa E501
                    f'https://auth.globus.org/scopes/{CLIENT_ID}/launch '
                    f'https://auth.globus.org/scopes/{CLIENT_ID}/launch',
                },
            ),
        ],
        metadata={
            'service': 'transfer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'access_token': _token,
            'scope': _scope,
        },
    ),
    dependent=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        status=200,
        json={
            'access_token': _token,
            'scope': _scope,
            'expires_in': 172800,
            'token_type': 'Bearer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'other_tokens': [],
        },
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'refresh_token': 'academyRefreshToken',
                    'grant_type': 'refresh_token',
                },
            ),
        ],
        metadata={
            'service': 'transfer',
            'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            'access_token': _token,
            'scope': _scope,
        },
    ),
)

register_response_set('auth.oauth2_client_credentials_tokens', RESPONSES)
