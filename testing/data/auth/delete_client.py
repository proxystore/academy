from __future__ import annotations

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set

from testing.constant import TEST_PROJECT_ID
from testing.data.auth._common import CLIENT_ID

CLIENT = {
    'required_idp': None,
    'name': 'Great client of FOO',
    'redirect_uris': [],
    'links': {'privacy_policy': None, 'terms_and_conditions': None},
    'scopes': [],
    'grant_types': [
        'authorization_code',
        'client_credentials',
        'refresh_token',
    ],
    'id': CLIENT_ID,
    'prompt_for_named_grant': False,
    'fqdns': ['globus.org'],
    'project': TEST_PROJECT_ID,
    'client_type': 'hybrid_confidential_client_resource_server',
    'visibility': 'private',
    'parent_client': None,
    'userinfo_from_effective_identity': True,
    'preselect_idp': None,
    'public_client': False,
}

RESPONSES = ResponseSet(
    default=RegisteredResponse(
        service='auth',
        method='DELETE',
        path=f'/v2/api/clients/{CLIENT["id"]}',
        json={'client': CLIENT},
        metadata={
            'client_id': CLIENT['id'],
        },
    ),
)

register_response_set('auth.delete_client', RESPONSES)
