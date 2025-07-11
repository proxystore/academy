"""HTTP message exchange client and server.

To start the exchange:
```bash
python -m academy.exchange.cloud --config exchange.yaml
```

Connect to the exchange through the client.
```python
from academy.exchange.cloud.client import HttpExchangeFactory

with HttpExchangeFactory(
    'http://localhost:1234'
).create_user_client() as exchange:
    aid, agent_info = exchange.register_agent()
    ...
```
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import enum
import logging
import ssl
import sys
import uuid
from collections.abc import Awaitable
from collections.abc import Sequence
from typing import Any
from typing import Callable

if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
    from asyncio import Queue
    from asyncio import QueueEmpty
    from asyncio import QueueShutDown

    AsyncQueue = Queue
else:  # pragma: <3.13 cover
    # Use of queues here is isolated to a single thread/event loop so
    # we only need culsans queues for the backport of shutdown() agent
    from culsans import AsyncQueue
    from culsans import AsyncQueueEmpty as QueueEmpty
    from culsans import AsyncQueueShutDown as QueueShutDown
    from culsans import Queue

from aiohttp.web import AppKey
from aiohttp.web import Application
from aiohttp.web import json_response
from aiohttp.web import middleware
from aiohttp.web import Request
from aiohttp.web import Response
from aiohttp.web import run_app
from pydantic import TypeAdapter
from pydantic import ValidationError

from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange import MailboxStatus
from academy.exchange.cloud.authenticate import Authenticator
from academy.exchange.cloud.authenticate import get_authenticator
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.config import ExchangeServingConfig
from academy.exchange.cloud.exceptions import ForbiddenError
from academy.exchange.cloud.exceptions import UnauthorizedError
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.logging import init_logging
from academy.message import ErrorResponse
from academy.message import Message

logger = logging.getLogger(__name__)


class StatusCode(enum.Enum):
    """Http status codes."""

    OKAY = 200
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    TIMEOUT = 408
    TERMINATED = 419
    NO_RESPONSE = 444


class _MailboxManager:
    def __init__(self) -> None:
        self._owners: dict[EntityId, str | None] = {}
        self._mailboxes: dict[EntityId, AsyncQueue[Message[Any]]] = {}
        self._terminated: set[EntityId] = set()
        self._agents: dict[AgentId[Any], tuple[str, ...]] = {}
        self._locks: dict[EntityId, asyncio.Lock] = {}

    def has_permissions(
        self,
        client: str | None,
        entity: EntityId,
    ) -> bool:
        return entity not in self._owners or self._owners[entity] == client

    async def check_mailbox(
        self,
        client: str | None,
        uid: EntityId,
    ) -> MailboxStatus:
        if uid not in self._mailboxes:
            return MailboxStatus.MISSING
        elif not self.has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        async with self._locks[uid]:
            if uid in self._terminated:
                return MailboxStatus.TERMINATED
            else:
                return MailboxStatus.ACTIVE

    def create_mailbox(
        self,
        client: str | None,
        uid: EntityId,
        agent: tuple[str, ...] | None = None,
    ) -> None:
        if not self.has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        mailbox = self._mailboxes.get(uid, None)
        if mailbox is None:
            if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
                queue: AsyncQueue[Message[Any]] = Queue()
            else:  # pragma: <3.13 cover
                queue: AsyncQueue[Message[Any]] = Queue().async_q
            self._mailboxes[uid] = queue
            self._terminated.discard(uid)
            self._owners[uid] = client
            self._locks[uid] = asyncio.Lock()
            if agent is not None and isinstance(uid, AgentId):
                self._agents[uid] = agent
            logger.info('Created mailbox for %s', uid)

    async def terminate(self, client: str | None, uid: EntityId) -> None:
        if not self.has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        self._terminated.add(uid)
        mailbox = self._mailboxes.get(uid, None)
        if mailbox is None:
            return

        async with self._locks[uid]:
            messages = await _drain_queue(mailbox)
            for message in messages:
                if message.is_request():
                    error = MailboxTerminatedError(uid)
                    body = ErrorResponse(exception=error)
                    response = message.create_response(body)
                    with contextlib.suppress(Exception):
                        await self.put(client, response)

            mailbox.shutdown(immediate=True)
            logger.info('Closed mailbox for %s', uid)

    async def discover(
        self,
        client: str | None,
        agent: str,
        allow_subclasses: bool,
    ) -> list[AgentId[Any]]:
        found: list[AgentId[Any]] = []
        for aid, agents in self._agents.items():
            if not self.has_permissions(client, aid):
                continue
            if aid in self._terminated:
                continue
            if agent == agents[0] or (allow_subclasses and agent in agents):
                found.append(aid)
        return found

    async def get(
        self,
        client: str | None,
        uid: EntityId,
        *,
        timeout: float | None = None,
    ) -> Message[Any]:
        if not self.has_permissions(client, uid):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        try:
            queue = self._mailboxes[uid]
        except KeyError as e:
            raise BadEntityIdError(uid) from e
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except QueueShutDown:
            raise MailboxTerminatedError(uid) from None

    async def put(self, client: str | None, message: Message[Any]) -> None:
        if not self.has_permissions(client, message.dest):
            raise ForbiddenError(
                'Client does not have correct permissions.',
            )

        try:
            queue = self._mailboxes[message.dest]
        except KeyError as e:
            raise BadEntityIdError(message.dest) from e

        async with self._locks[message.dest]:
            try:
                await queue.put(message)
            except QueueShutDown:
                raise MailboxTerminatedError(message.dest) from None


async def _drain_queue(queue: AsyncQueue[Message[Any]]) -> list[Message[Any]]:
    items: list[Message[Any]] = []

    while True:
        try:
            item = queue.get_nowait()
        except (QueueShutDown, QueueEmpty):
            break
        else:
            items.append(item)
            queue.task_done()

    return items


MANAGER_KEY = AppKey('manager', _MailboxManager)


async def _create_mailbox_route(request: Request) -> Response:
    data = await request.json()
    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        raw_mailbox_id = data['mailbox']
        mailbox_id: EntityId = TypeAdapter(EntityId).validate_json(
            raw_mailbox_id,
        )
        agent_raw = data.get('agent', None)
        agent = agent_raw.split(',') if agent_raw is not None else None
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid mailbox ID',
        )

    client_id = request.headers.get('client_id', None)
    try:
        manager.create_mailbox(client_id, mailbox_id, agent)
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    return Response(status=StatusCode.OKAY.value)


async def _terminate_route(request: Request) -> Response:
    data = await request.json()
    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        raw_mailbox_id = data['mailbox']
        mailbox_id: EntityId = TypeAdapter(EntityId).validate_json(
            raw_mailbox_id,
        )
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid mailbox ID',
        )

    client_id = request.headers.get('client_id', None)
    try:
        await manager.terminate(client_id, mailbox_id)
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    return Response(status=StatusCode.OKAY.value)


async def _discover_route(request: Request) -> Response:
    data = await request.json()
    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        agent = data['agent']
        allow_subclasses = data['allow_subclasses']
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid arguments',
        )

    client_id = request.headers.get('client_id', None)
    agent_ids = await manager.discover(
        client_id,
        agent,
        allow_subclasses,
    )

    return json_response(
        {'agent_ids': ','.join(str(aid.uid) for aid in agent_ids)},
    )


async def _check_mailbox_route(request: Request) -> Response:
    data = await request.json()
    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        raw_mailbox_id = data['mailbox']
        mailbox_id: EntityId = TypeAdapter(EntityId).validate_json(
            raw_mailbox_id,
        )
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid mailbox ID',
        )

    client_id = request.headers.get('client_id', None)
    try:
        status = await manager.check_mailbox(client_id, mailbox_id)
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    return json_response({'status': status.value})


async def _send_message_route(request: Request) -> Response:
    data = await request.json()
    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        raw_message = data.get('message')
        message: Message[Any] = Message.model_validate_json(raw_message)
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid message',
        )

    client_id = request.headers.get('client_id', None)
    try:
        await manager.put(client_id, message)
    except BadEntityIdError:
        return Response(
            status=StatusCode.NOT_FOUND.value,
            text='Unknown mailbox ID',
        )
    except MailboxTerminatedError:
        return Response(
            status=StatusCode.TERMINATED.value,
            text='Mailbox was closed',
        )
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    else:
        return Response(status=StatusCode.OKAY.value)


async def _recv_message_route(request: Request) -> Response:  # noqa: PLR0911
    try:
        data = await request.json()
    except ConnectionResetError:  # pragma: no cover
        # This happens when the client cancel's it's listener task, which is
        # waiting on recv, because the client is shutting down and closing
        # its connection. In this case, we don't need to do anything
        # because the client disconnected itself. If we don't catch this
        # error, aiohttp will just log an error message each time this happens.
        return Response(status=StatusCode.NO_RESPONSE.value)

    manager: _MailboxManager = request.app[MANAGER_KEY]

    try:
        raw_mailbox_id = data['mailbox']
        mailbox_id: EntityId = TypeAdapter(EntityId).validate_json(
            raw_mailbox_id,
        )
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid mailbox ID',
        )

    timeout = data.get('timeout', None)

    try:
        client_id = request.headers.get('client_id', None)
        message = await manager.get(client_id, mailbox_id, timeout=timeout)
    except BadEntityIdError:
        return Response(
            status=StatusCode.NOT_FOUND.value,
            text='Unknown mailbox ID',
        )
    except MailboxTerminatedError:
        return Response(
            status=StatusCode.TERMINATED.value,
            text='Mailbox was closed',
        )
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    except asyncio.TimeoutError:
        return Response(
            status=StatusCode.TIMEOUT.value,
            text='Request timeout',
        )
    else:
        return json_response({'message': message.model_dump_json()})


def authenticate_factory(
    authenticator: Authenticator,
) -> Any:
    """Create an authentication middleware for a given authenticator.

    Args:
        authenticator: Used to validate client id and transform token into id.

    Returns:
        A aiohttp.web.middleware function that will only allow authenticated
            requests.
    """

    @middleware
    async def authenticate(
        request: Request,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        loop = asyncio.get_running_loop()
        try:
            # Needs to be run in executor because globus client is blocking
            client_uuid: uuid.UUID = await loop.run_in_executor(
                None,
                authenticator.authenticate_user,
                request.headers,
            )
        except ForbiddenError:
            return Response(
                status=StatusCode.FORBIDDEN.value,
                text='Token expired or revoked.',
            )
        except UnauthorizedError:
            return Response(
                status=StatusCode.UNAUTHORIZED.value,
                text='Missing required headers.',
            )

        headers = request.headers.copy()
        headers['client_id'] = str(client_uuid)

        # Handle early client-side disconnect in Issue #142
        # This is somewhat hard to reproduce in tests:
        # https://github.com/aio-libs/aiohttp/issues/6978
        if (
            request.transport is None or request.transport.is_closing()
        ):  # pragma: no cover
            return Response(status=StatusCode.NO_RESPONSE.value)

        request = request.clone(headers=headers)
        return await handler(request)

    return authenticate


def create_app(
    auth_config: ExchangeAuthConfig | None = None,
) -> Application:
    """Create a new server application."""
    middlewares = []
    if auth_config is not None:
        authenticator = get_authenticator(auth_config)
        middlewares.append(authenticate_factory(authenticator))

    manager = _MailboxManager()
    app = Application(middlewares=middlewares)
    app[MANAGER_KEY] = manager

    app.router.add_post('/mailbox', _create_mailbox_route)
    app.router.add_delete('/mailbox', _terminate_route)
    app.router.add_get('/mailbox', _check_mailbox_route)
    app.router.add_put('/message', _send_message_route)
    app.router.add_get('/message', _recv_message_route)
    app.router.add_get('/discover', _discover_route)

    return app


def _run(
    config: ExchangeServingConfig,
) -> None:
    app = create_app(config.auth)
    init_logging(config.log_level, logfile=config.log_file)
    logger = logging.getLogger('root')
    logger.info(
        'Exchange listening on %s:%s (ctrl-C to exit)',
        config.host,
        config.port,
    )

    ssl_context: ssl.SSLContext | None = None
    if config.certfile is not None:  # pragma: no cover
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(config.certfile, keyfile=config.keyfile)

    run_app(
        app,
        host=config.host,
        port=config.port,
        print=None,
        ssl_context=ssl_context,
    )
    logger.info('Exchange closed!')


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)

    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)

    server_config = ExchangeServingConfig.from_toml(args.config)
    _run(server_config)

    return 0
