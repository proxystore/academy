from __future__ import annotations

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set

from academy.exchange.cloud.globus import AcademyGlobusClient

RESPONSES = ResponseSet(
    default=RegisteredResponse(
        path=f'{AcademyGlobusClient.base_url}/mailbox',
        method='DELETE',
        status=200,
    ),
)

register_response_set(AcademyGlobusClient.terminate, RESPONSES)
