from __future__ import annotations

import pickle
from typing import Any

import pytest

from academy.identifier import AgentId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import ErrorResponse
from academy.message import Header
from academy.message import Message
from academy.message import PingRequest
from academy.message import ShutdownRequest
from academy.message import SuccessResponse


@pytest.mark.parametrize(
    'message_body',
    (
        ActionRequest(action='foo', pargs=(b'bar',)),
        PingRequest(),
        ShutdownRequest(),
    ),
)
def test_request_message(message_body: Any) -> None:
    message = Message.create(
        src=AgentId.new(),
        dest=AgentId.new(),
        body=message_body,
    )
    assert isinstance(str(message), str)
    assert isinstance(repr(message), str)
    jsoned = message.model_dump_json()
    recreated: Message[Any] = Message.model_validate_json(jsoned)
    assert message == recreated
    assert hash(message) == hash(recreated)
    assert message != object()
    pickled = message.model_serialize()
    recreated = Message.model_deserialize(pickled)
    assert message == recreated


@pytest.mark.parametrize(
    'message_body',
    (
        ActionResponse(action='foo', result=b'bar'),
        ErrorResponse(exception=Exception()),
        SuccessResponse(),
    ),
)
def test_response_message(message_body: Any) -> None:
    header = Header(src=AgentId.new(), dest=AgentId.new(), kind='response')
    message: Message[Any] = Message(header=header, body=message_body)
    assert isinstance(str(message), str)
    assert isinstance(repr(message), str)
    jsoned = message.model_dump_json()
    recreated: Message[Any] = Message.model_validate_json(jsoned)
    assert message == recreated
    pickled = message.model_serialize()
    recreated = Message.model_deserialize(pickled)
    assert message == recreated


def test_deserialize_bad_type() -> None:
    pickled = pickle.dumps('string')
    with pytest.raises(
        TypeError,
        match='Deserialized message is not of type Message.',
    ):
        Message.model_deserialize(pickled)


def tests_create_response_from_response_error() -> None:
    message = Message.create(
        src=AgentId.new(),
        dest=AgentId.new(),
        body=SuccessResponse(),
    )
    with pytest.raises(
        ValueError,
        match='Cannot create response header from another response',
    ):
        message.create_response(SuccessResponse())


def test_action_request_lazy_deserialize() -> None:
    request = ActionRequest(action='foo', pargs=('bar',), kargs={'foo': 'bar'})

    json = request.model_dump_json()
    reconstructed = ActionRequest.model_validate_json(json)

    assert isinstance(reconstructed, ActionRequest)
    assert isinstance(reconstructed.pargs, str)
    assert isinstance(reconstructed.kargs, str)

    reconstructed.get_args()
    reconstructed.get_kwargs()

    assert isinstance(reconstructed.pargs, tuple)
    assert isinstance(reconstructed.kargs, dict)


def test_action_response_lazy_deserialize() -> None:
    response = ActionResponse(action='foo', result={'foo': 'bar'})

    json = response.model_dump_json()
    reconstructed = ActionResponse.model_validate_json(json)

    assert isinstance(reconstructed, ActionResponse)
    assert isinstance(reconstructed.result, list)

    reconstructed.get_result()

    assert isinstance(reconstructed.result, dict)


def test_error_response_lazy_deserialize() -> None:
    response = ErrorResponse(exception=Exception('Oops!'))

    json = response.model_dump_json()
    reconstructed = ErrorResponse.model_validate_json(json)

    assert isinstance(reconstructed, ErrorResponse)
    assert isinstance(reconstructed.exception, str)

    reconstructed.get_exception()

    assert isinstance(reconstructed.exception, Exception)
