from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from academy.exception import BadEntityIdError
from academy.exception import ForbiddenError
from academy.exception import MailboxTerminatedError
from academy.exception import MessageTooLargeError
from academy.exchange import MailboxStatus
from academy.exchange.cloud.backend import MailboxBackend
from academy.exchange.cloud.backend import PythonBackend
from academy.exchange.cloud.backend import RedisBackend
from academy.identifier import UserId
from academy.message import Message
from academy.message import PingRequest

BACKEND_TYPES = (PythonBackend, RedisBackend)


@pytest_asyncio.fixture(params=BACKEND_TYPES)
async def backend(request, mock_redis) -> AsyncGenerator[MailboxBackend]:
    return request.param()


@pytest.mark.asyncio
async def test_mailbox_backend_create_close(backend: MailboxBackend) -> None:
    user_id = str(uuid.uuid4())
    uid = UserId.new()
    # Should do nothing since mailbox doesn't exist
    await backend.terminate(user_id, uid)
    assert await backend.check_mailbox(user_id, uid) == MailboxStatus.MISSING
    await backend.create_mailbox(user_id, uid)
    assert await backend.check_mailbox(user_id, uid) == MailboxStatus.ACTIVE
    await backend.create_mailbox(user_id, uid)  # Idempotent check

    bad_user = str(uuid.uuid4())  # Authentication check
    with pytest.raises(ForbiddenError):
        await backend.create_mailbox(bad_user, uid)
    with pytest.raises(ForbiddenError):
        await backend.check_mailbox(bad_user, uid)
    with pytest.raises(ForbiddenError):
        await backend.terminate(bad_user, uid)

    await backend.terminate(user_id, uid)
    await backend.terminate(user_id, uid)  # Idempotent check


@pytest.mark.asyncio
async def test_mailbox_backend_send_recv(backend: MailboxBackend) -> None:
    user_id = str(uuid.uuid4())
    bad_user = str(uuid.uuid4())
    uid = UserId.new()
    await backend.create_mailbox(user_id, uid)

    message = Message.create(src=uid, dest=uid, body=PingRequest())
    with pytest.raises(ForbiddenError):
        await backend.put(bad_user, message)
    await backend.put(user_id, message)

    with pytest.raises(ForbiddenError):
        await backend.get(bad_user, uid)
    assert await backend.get(user_id, uid) == message

    await backend.terminate(user_id, uid)


@pytest.mark.asyncio
async def test_mailbox_backend_bad_identifier(backend: MailboxBackend) -> None:
    uid = UserId.new()
    message = Message.create(src=uid, dest=uid, body=PingRequest())
    with pytest.raises(BadEntityIdError):
        await backend.get(None, uid)

    with pytest.raises(BadEntityIdError):
        await backend.put(None, message)


@pytest.mark.asyncio
async def test_mailbox_backend_mailbox_closed(backend: MailboxBackend) -> None:
    uid = UserId.new()
    await backend.create_mailbox(None, uid)
    await backend.terminate(None, uid)
    message = Message.create(src=uid, dest=uid, body=PingRequest())
    with pytest.raises(MailboxTerminatedError):
        await backend.get(None, uid)

    with pytest.raises(MailboxTerminatedError):
        await backend.put(None, message)


@pytest.mark.asyncio
async def test_redis_backend_message_size(mock_redis) -> None:
    backend = RedisBackend(message_size_limit_kb=0)
    uid = UserId.new()
    await backend.create_mailbox(None, uid)
    message = Message.create(src=uid, dest=uid, body=PingRequest())
    with pytest.raises(MessageTooLargeError):
        await backend.put(None, message)
