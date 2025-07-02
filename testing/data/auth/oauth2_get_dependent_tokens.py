from __future__ import annotations

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set
from responses import matchers

RESPONSES = ResponseSet(
    default=RegisteredResponse(
        service='auth',
        path='/v2/oauth2/token',
        method='POST',
        json=[
            {
                'scope': 'https://auth.globus.org/scopes/a7e16357-8edf-414d-9e73-85e4b0b18be4/academy_exchange',
                'access_token': 'academyToken',
                'refresh_token': 'academyRefreshToken',
                'token_type': 'bearer',
                'expires_in': 120,
                'resource_server': 'a7e16357-8edf-414d-9e73-85e4b0b18be4',
            },
        ],
        match=[
            matchers.urlencoded_params_matcher(
                {
                    'grant_type': 'urn:globus:auth:grant_type:dependent_token',
                    'token': 'Bearer DUMMY_TRANSFER_TOKEN_FROM_THE_INTERTUBES',
                    'access_type': 'offline',
                },
            ),
        ],
        metadata={
            'rs_data': {
                'a7e16357-8edf-414d-9e73-85e4b0b18be4': {
                    'access_token': 'academyToken',
                    'refresh_token': 'academyRefreshToken',
                    'scope': 'https://auth.globus.org/scopes/a7e16357-8edf-414d-9e73-85e4b0b18be4/academy_exchange',
                },
            },
        },
    ),
)

register_response_set('auth.oauth2_get_dependent_tokens', RESPONSES)
