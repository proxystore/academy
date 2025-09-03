from __future__ import annotations

import os
from typing import Any

import pytest
import responses
from globus_sdk._testing import load_response

from academy.exchange.cloud.app import StatusCode
from academy.exchange.cloud.globus import AcademyGlobusClient
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.message import Message
from academy.message import PingRequest
from testing.agents import EmptyAgent


@pytest.fixture(autouse=True)
def mocked_responses(monkeypatch):
    responses.start()
    monkeypatch.setitem(os.environ, 'GLOBUS_SDK_ENVIRONMENT', 'production')
    yield
    responses.stop()
    responses.reset()


@pytest.fixture
def academy_client():
    return AcademyGlobusClient()


def test_globus_client_discover(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.discover)
    response = academy_client.discover(EmptyAgent)
    assert response.http_status == StatusCode.OKAY.value
    assert 'agent_ids' in response.data


def test_globus_client_recv(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.recv)
    response = academy_client.recv(UserId.new())
    assert response.http_status == StatusCode.OKAY.value
    assert 'message' in response.data


def test_globus_client_register_agent(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.register_agent)
    response = academy_client.register_agent(AgentId.new(), EmptyAgent)
    assert response.http_status == StatusCode.OKAY.value


def test_globus_client_register_client(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.register_client)
    response = academy_client.register_client(UserId.new())
    assert response.http_status == StatusCode.OKAY.value


def test_globus_client_send(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.send)
    user = UserId.new()
    agent: AgentId[Any] = AgentId.new()
    message = Message.create(
        src=user,
        dest=agent,
        body=PingRequest(),
    )
    response = academy_client.send(message)
    assert response.http_status == StatusCode.OKAY.value


def test_globus_client_status(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.status)
    agent: AgentId[Any] = AgentId.new()
    response = academy_client.status(agent)
    assert response.http_status == StatusCode.OKAY.value
    assert 'status' in response.data


def test_globus_client_terminate(academy_client: AcademyGlobusClient):
    load_response(AcademyGlobusClient.terminate)
    agent: AgentId[Any] = AgentId.new()
    response = academy_client.terminate(agent)
    assert response.http_status == StatusCode.OKAY.value
