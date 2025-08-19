from __future__ import annotations

import typing as t
import uuid

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set
from responses.matchers import json_params_matcher

from testing.constant import TEST_PROJECT_ID
from testing.data.auth._common import CLIENT_ID

_COMMON_RESPONSE_RECORD: dict[str, t.Any] = {
    'fqdns': [],
    'links': {'privacy_policy': None, 'terms_and_conditions': None},
    'parent_client': None,
    'preselect_idp': None,
    'prompt_for_named_grant': True,
    'redirect_uris': [],
    'required_idp': None,
    'scopes': [],
    'userinfo_from_effective_identity': True,
    'id': CLIENT_ID,
}

PUBLIC_CLIENT_RESPONSE_RECORD = {
    'grant_types': ['authorization_code', 'refresh_token'],
    **_COMMON_RESPONSE_RECORD,
}

PRIVATE_CLIENT_RESPONSE_RECORD = {
    'grant_types': [
        'authorization_code',
        'client_credentials',
        'refresh_token',
        'urn:globus:auth:grant_type:dependent_token',
    ],
    **_COMMON_RESPONSE_RECORD,
}

PUBLIC_CLIENT_REQUEST_ARGS = {
    'project': TEST_PROJECT_ID,
}


def register_response(
    args: t.Mapping[str, t.Any],
) -> RegisteredResponse:
    request_args = {
        **PUBLIC_CLIENT_REQUEST_ARGS,
        **args,
    }
    # Default to public_client=True
    if 'public_client' not in args and 'client_type' not in args:
        request_args['public_client'] = True

    # The request body follows almost directly from the request args.
    # Some name of args to create_client() have differently named
    # fields.
    request_body: dict[str, t.Any] = {}
    for arg_name, arg_body in request_args.items():
        if arg_name in {'terms_and_conditions', 'privacy_policy'}:
            request_body['links'] = {
                arg_name: arg_body,
                **request_body.get('links', {}),
            }
        else:
            request_body[arg_name] = arg_body

    client_response_record = {}
    if (
        request_args.get('public_client', False)
        or request_args.get('client_type') == 'public_installed_client'
    ):
        client_response_record = {
            **PUBLIC_CLIENT_RESPONSE_RECORD,
            **request_body,
        }
        if 'client_type' not in client_response_record:
            client_response_record['client_type'] = 'public_installed_client'
    else:
        client_response_record = {
            **PRIVATE_CLIENT_RESPONSE_RECORD,
            **request_body,
        }
        if 'client_type' not in client_response_record:
            client_response_record['client_type'] = (
                'hybrid_confidential_client_resource_server'
            )

    return RegisteredResponse(
        service='auth',
        method='POST',
        path='/v2/api/clients',
        json={'client': client_response_record},
        metadata={
            # Test functions use 'args' to form request
            'args': request_args,
            # Test functions use 'response' to verify response
            'response': client_response_record,
        },
        match=[
            json_params_matcher({'client': request_body}, strict_match=False),
        ],
    )


RESPONSES = ResponseSet(
    default=register_response({}),
    name=register_response({'name': str(uuid.uuid4()).replace('-', '')}),
    public_client=register_response({'public_client': True}),
    private_client=register_response({'public_client': False}),
    project_id=register_response({'project': str(uuid.uuid1())}),
    publicly_visible=register_response({'visibility': 'public'}),
    not_publicly_visible=register_response({'visibility': 'private'}),
    redirect_uris=register_response({'redirect_uris': ['https://foo.com']}),
    links=register_response(
        {
            'terms_and_conditions': 'https://foo.org',
            'privacy_policy': 'https://boo.org',
        },
    ),
    required_idp=register_response({'required_idp': str(uuid.uuid1())}),
    preselect_idp=register_response({'preselect_idp': str(uuid.uuid1())}),
    client_type_confidential_client=register_response(
        {'client_type': 'confidential_client'},
    ),
    client_type_public_installed_client=register_response(
        {'client_type': 'public_installed_client'},
    ),
    client_type_client_identity=register_response(
        {'client_type': 'client_identity'},
    ),
    client_type_resource_server=register_response(
        {'client_type': 'resource_server'},
    ),
    client_type_globus_connect_server=register_response(
        {'client_type': 'globus_connect_server'},
    ),
    client_type_hybrid_confidential_client_resource_server=register_response(
        {'client_type': 'hybrid_confidential_client_resource_server'},
    ),
    client_type_public_webapp_client=register_response(
        {'client_type': 'public_webapp_client'},
    ),
)

register_response_set('auth.create_client', RESPONSES)
