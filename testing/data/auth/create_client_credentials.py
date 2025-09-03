from __future__ import annotations

import uuid

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set

from testing.data.auth._common import CLIENT_ID

NEW_CREDENTIAL_NAME = str(uuid.uuid4()).replace('-', '')

CREDENTIAL = {
    'name': 'foo',
    'id': str(uuid.uuid1()),
    'created': '2023-10-21T22:46:15.845937+00:00',
    'client': CLIENT_ID,
    'secret': 'abc123',
}


RESPONSES = ResponseSet(
    default=RegisteredResponse(
        service='auth',
        method='POST',
        path=f'/v2/api/clients/{CREDENTIAL["client"]}/credentials',
        json={'credential': CREDENTIAL},
        metadata={
            'credential_id': CREDENTIAL['id'],
            'client_id': CREDENTIAL['client'],
            'name': CREDENTIAL['name'],
        },
    ),
    name=RegisteredResponse(
        service='auth',
        method='POST',
        path=f'/v2/api/clients/{CREDENTIAL["client"]}/credentials',
        json={
            'credential': {
                **CREDENTIAL,
                'name': NEW_CREDENTIAL_NAME,
            },
        },
        metadata={
            'name': NEW_CREDENTIAL_NAME,
            'client_id': CREDENTIAL['client'],
        },
    ),
)

register_response_set('auth.create_client_credentials', RESPONSES)
