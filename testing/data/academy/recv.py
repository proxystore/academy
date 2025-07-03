from __future__ import annotations

from globus_sdk._testing.models import RegisteredResponse
from globus_sdk._testing.models import ResponseSet
from globus_sdk._testing.registry import register_response_set

from academy.exchange.cloud.globus import AcademyGlobusClient
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.message import PingResponse
from testing.constant import TEST_ACADEMY_PATH

RESPONSES = ResponseSet(
    default=RegisteredResponse(
        path=f'{TEST_ACADEMY_PATH}/message',
        method='GET',
        json={
            'message': PingResponse(
                src=AgentId.new(),
                dest=UserId.new(),
            ).model_dump_json(),
        },
        status=200,
    ),
)

register_response_set(AcademyGlobusClient.recv, RESPONSES)
