"""HTTP message exchange client and server.

To start the exchange:
```bash
python -m academy.exchange.cloud --config exchange.toml
```

Connect to the exchange through the client.
```python
from academy.exchange import HttpExchangeFactory

with HttpExchangeFactory(
    'http://localhost:1234'
).create_user_client() as exchange:
    aid, agent_info = exchange.register_agent()
    ...
```
"""

from __future__ import annotations

import argparse
import enum
import logging
import ssl
import sys
from collections.abc import Awaitable
from collections.abc import Sequence
from typing import Any
from typing import Callable

if sys.version_info >= (3, 13):  # pragma: >=3.13 cover
    from asyncio import Queue

    AsyncQueue = Queue
else:  # pragma: <3.13 cover
    # Use of queues here is isolated to a single thread/event loop so
    # we only need culsans queues for the backport of shutdown() agent
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
from academy.exception import ForbiddenError
from academy.exception import MailboxTerminatedError
from academy.exception import MessageTooLargeError
from academy.exception import UnauthorizedError
from academy.exchange.cloud.authenticate import Authenticator
from academy.exchange.cloud.authenticate import get_authenticator
from academy.exchange.cloud.backend import MailboxBackend
from academy.exchange.cloud.backend import PythonBackend
from academy.exchange.cloud.config import BackendConfig
from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.config import ExchangeServingConfig
from academy.identifier import EntityId
from academy.logging import init_logging
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
    TOO_LARGE = 413
    TERMINATED = 419
    NO_RESPONSE = 444


MANAGER_KEY = AppKey('manager', MailboxBackend)


async def _create_mailbox_route(request: Request) -> Response:
    data = await request.json()
    manager: MailboxBackend = request.app[MANAGER_KEY]

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

    client_id = request.headers.get('client_id', '')
    try:
        await manager.create_mailbox(client_id, mailbox_id, agent)
    except ForbiddenError:
        return Response(
            status=StatusCode.FORBIDDEN.value,
            text='Incorrect permissions',
        )
    return Response(status=StatusCode.OKAY.value)


async def _terminate_route(request: Request) -> Response:
    data = await request.json()
    manager: MailboxBackend = request.app[MANAGER_KEY]

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

    client_id = request.headers.get('client_id', '')
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
    manager: MailboxBackend = request.app[MANAGER_KEY]

    try:
        agent = data['agent']
        allow_subclasses = data['allow_subclasses']
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid arguments',
        )

    client_id = request.headers.get('client_id', '')
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
    manager: MailboxBackend = request.app[MANAGER_KEY]

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

    client_id = request.headers.get('client_id', '')
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
    manager: MailboxBackend = request.app[MANAGER_KEY]

    try:
        raw_message = data.get('message')
        message: Message[Any] = Message.model_validate_json(raw_message)
    except (KeyError, ValidationError):
        return Response(
            status=StatusCode.BAD_REQUEST.value,
            text='Missing or invalid message',
        )

    client_id = request.headers.get('client_id', '')
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
    except MessageTooLargeError as e:
        return Response(
            status=StatusCode.TOO_LARGE.value,
            text=f'Message of size {e.size} larger than limit {e.limit}.',
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

    manager: MailboxBackend = request.app[MANAGER_KEY]

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
        client_id = request.headers.get('client_id', '')
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
    except TimeoutError:
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
        try:
            client_id: str = await authenticator.authenticate_user(
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
        headers['client_id'] = client_id

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
    backend_config: BackendConfig | None = None,
    auth_config: ExchangeAuthConfig | None = None,
) -> Application:
    """Create a new server application."""
    if backend_config is not None:
        backend = backend_config.get_backend()
    else:
        backend = PythonBackend()

    middlewares = []
    if auth_config is not None:
        authenticator = get_authenticator(auth_config)
        middlewares.append(authenticate_factory(authenticator))

    app = Application(middlewares=middlewares)
    app[MANAGER_KEY] = backend

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
    app = create_app(config.backend, config.auth)
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
