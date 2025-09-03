from __future__ import annotations

import typing as t
import uuid

from globus_sdk import DependentScopeSpec
from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set
from responses.matchers import json_params_matcher

from testing.data.auth._common import CLIENT_ID

SCOPE_REQUEST_ARGS = {
    'client_id': CLIENT_ID,
    'name': 'Agent launch',
    'description': 'Launch agent',
    'scope_suffix': 'launch',
}


def make_request_body(
    request_args: t.Mapping[str, t.Any],
) -> dict[str, t.Any]:
    request_body = {
        'name': request_args['name'],
        'description': request_args['description'],
        'scope_suffix': request_args['scope_suffix'],
    }

    if 'advertised' in request_args:
        request_body['advertised'] = request_args['advertised']

    if 'allows_refresh_token' in request_args:
        request_body['allows_refresh_token'] = request_args[
            'allows_refresh_token'
        ]

    if 'required_domains' in request_args:
        request_body['required_domains'] = request_args['required_domains']

    if 'dependent_scopes' in request_args:
        request_body['dependent_scopes'] = request_args['dependent_scopes']

    return request_body


def make_response_body(
    request_args: t.Mapping[str, t.Any],
) -> dict[str, t.Any]:
    return {
        'scope_string': f'https://auth.globus.org/scopes/{request_args["client_id"]}/{request_args["scope_suffix"]}',
        'allows_refresh_token': request_args.get('allows_refresh_token', True),
        'id': str(uuid.uuid1()),
        'advertised': request_args.get('advertised', False),
        'required_domains': request_args.get('required_domains', []),
        'name': request_args['name'],
        'description': request_args['description'],
        'client': str(request_args['client_id']),
        'dependent_scopes': [
            {
                'scope': str(ds['scope']),
                'optional': ds['optional'],
                'requires_refresh_token': ds['requires_refresh_token'],
            }
            for ds in request_args.get('dependent_scopes', [])
        ],
    }


def register_response(
    args: t.Mapping[str, t.Any],
) -> RegisteredResponse:
    request_args = {**SCOPE_REQUEST_ARGS, **args}
    request_body = make_request_body(request_args)
    response_body = make_response_body(request_args)

    return RegisteredResponse(
        service='auth',
        method='POST',
        path=f'/v2/api/clients/{request_args["client_id"]}/scopes',
        json={'scopes': [response_body]},
        metadata={
            # Test functions use 'args' to form request
            'args': request_args,
            # Test functions use 'response' to verify response
            'response': response_body,
        },
        match=[
            json_params_matcher({'scope': request_body}, strict_match=False),
        ],
    )


RESPONSES = ResponseSet(
    default=register_response({}),
    client_id_str=register_response({'client_id': CLIENT_ID}),
    client_id_uuid=register_response({'client_id': uuid.UUID(CLIENT_ID)}),
    name=register_response({'name': str(uuid.uuid4()).replace('-', '')}),
    description=register_response(
        {'description': str(uuid.uuid4()).replace('-', '')},
    ),
    scope_suffix=register_response(
        {'scope_suffix': str(uuid.uuid4()).replace('-', '')},
    ),
    advertised=register_response({'advertised': True}),
    not_advertised=register_response({'advertised': False}),
    allows_refresh_token=register_response({'allows_refresh_token': True}),
    disallows_refresh_token=register_response({'allows_refresh_token': False}),
    no_required_domains=register_response({'required_domains': []}),
    required_domains=register_response(
        {'required_domains': ['globus.org', 'uchicago.edu']},
    ),
    no_dependent_scopes=register_response({'dependent_scopes': []}),
    dependent_scopes=register_response(
        {
            'dependent_scopes': [
                DependentScopeSpec(str(uuid.uuid1()), True, True),
                DependentScopeSpec(uuid.uuid1(), False, False),
            ],
        },
    ),
)

register_response_set('auth.create_scope', RESPONSES)
