# ruff: noqa: D102
from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import ClassVar
from typing import Generic

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

import globus_sdk
from globus_sdk import AuthClient
from globus_sdk import DependentScopeSpec
from globus_sdk import GlobusApp
from globus_sdk import GlobusHTTPResponse
from globus_sdk import Scope
from globus_sdk.authorizers import GlobusAuthorizer
from globus_sdk.exc import GlobusAPIError
from globus_sdk.gare import GlobusAuthorizationParameters
from globus_sdk.scopes import AuthScopes

from academy.agent import Agent
from academy.agent import AgentT
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exception import UnauthorizedError
from academy.exchange import ExchangeFactory
from academy.exchange.cloud.app import StatusCode
from academy.exchange.cloud.login import get_globus_app
from academy.exchange.cloud.scopes import ACADEMY_EXCHANGE_SCOPE_ID
from academy.exchange.cloud.scopes import AcademyExchangeScopes
from academy.exchange.transport import ExchangeTransportMixin
from academy.exchange.transport import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId
from academy.message import Message
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)

ACADEMY_PROJECT = '6c357d85-dfb2-4f22-8ab3-dc617849aac5'


class AcademyAPIError(GlobusAPIError):
    """Error class to represent error responses from Academy."""


class AcademyGlobusClient(globus_sdk.BaseClient):
    """A globus service client to make requests to hosted exchange.

    The GlobusExchangeClient acts as a wrapper through which authenticated
    requests are issued. The exchange automatically handles things like
    retrying, refreshing tokens, and exponential backoff.
    The [`BaseClient`][globus_sdk.BaseClient] is implemented using requests,
    so calls are synchronous, and must be run within a thread when using
    asyncio.
    """

    # service_name = 'academy'
    base_url = 'http://0.0.0.0:8700'  # TODO: Get domain
    scopes = AcademyExchangeScopes
    default_scope_requirements: ClassVar[list[Scope]] = [
        Scope(AcademyExchangeScopes.academy_exchange),
    ]
    error_class = AcademyAPIError

    _mailbox_url = '/mailbox'
    _message_url = '/message'
    _discover_url = '/discover'

    def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> GlobusHTTPResponse:
        agent_str = f'{agent.__module__}.{agent.__name__}'
        return self.request(
            'GET',
            self._discover_url,
            data={
                'agent': agent_str,
                'allow_subclasses': allow_subclasses,
            },
        )

    def recv(
        self,
        mailbox_id: EntityId,
        timeout: float | None = None,
    ) -> GlobusHTTPResponse:
        return self.request(
            'GET',
            self._message_url,
            data={
                'mailbox': mailbox_id.model_dump_json(),
                'timeout': timeout,
            },
        )

    def register_agent(
        self,
        agent_id: AgentId[AgentT],
        agent: type[AgentT],
    ) -> GlobusHTTPResponse:
        return self.post(
            self._mailbox_url,
            data={
                'mailbox': agent_id.model_dump_json(),
                'agent': ','.join(agent._agent_mro()),
            },
        )

    def register_client(
        self,
        mailbox_id: EntityId,
    ) -> GlobusHTTPResponse:
        return self.post(
            self._mailbox_url,
            data={
                'mailbox': mailbox_id.model_dump_json(),
            },
        )

    def send(self, message: Message[Any]) -> GlobusHTTPResponse:
        return self.put(
            self._message_url,
            data={'message': message.model_dump_json()},
        )

    def status(self, uid: EntityId) -> GlobusHTTPResponse:
        return self.request(
            'GET',
            self._mailbox_url,
            data={'mailbox': uid.model_dump_json()},
        )

    def terminate(self, uid: EntityId) -> GlobusHTTPResponse:
        return self.request(
            'DELETE',
            self._mailbox_url,
            data={'mailbox': uid.model_dump_json()},
        )


@dataclasses.dataclass
class GlobusAgentRegistration(Generic[AgentT]):
    """Agent registration for hosted globus exchange."""

    agent_id: AgentId[AgentT]
    """Unique identifier for the agent created by the exchange."""

    client_id: uuid.UUID
    """Client ID of Globus resource server.

    Each agent is a resources server.  This allows the agent to exchange
    delegated tokens to act on the exchange on behalf of the client, and
    to create it's own delegated tokens so other agents can act on its
    behalf.
    """

    token: str
    """Auth. token provided by launching client (user or another agent)."""

    secret: str
    """Secret for agent to use to authenticate itself with Globus Auth.

    Agents are created as hybrid resource servers within the Globus ecosystem.
    This allows them to use delegated tokens, but also requires the them to
    be able to store secrets. In order to support the launching of agents,
    we pass a secret as part of the agent initialization. We assume the
    security of the launching channel (typically Globus Compute in the
    Academy ecosystem).
    """


class GlobusExchangeTransport(ExchangeTransportMixin, NoPickleMixin):
    """Globus exchange client.

    Args:
        mailbox_id: Identifier of the mailbox on the exchange. If there is
            not an id provided, the exchange will create a new client mailbox.
        project_id: Globus Identifier of project to create agents under.
        app: For user authorization through token retrieval
        authorizer: For service authorization through token retrieval
    """

    def __init__(
        self,
        mailbox_id: EntityId,
        *,
        project_id: uuid.UUID,
        app: GlobusApp | None = None,
        authorizer: GlobusAuthorizer | None,
    ) -> None:
        self._mailbox_id = mailbox_id
        self.project = project_id
        self.child_clients: list[uuid.UUID] = []

        self.login_time = datetime.min
        self._app_lock = threading.Lock()
        self._app = app
        self._authorizer = authorizer
        self._local_data = threading.local()
        self.executor = ThreadPoolExecutor(
            thread_name_prefix='exchange-globus-thread',
        )

    @property
    def exchange_client(self) -> AcademyGlobusClient:
        """A thread local copy of the Globus AuthClient."""
        try:
            return self._local_data.exchange_client
        except AttributeError:
            self._local_data.exchange_client = AcademyGlobusClient(
                app=self._app,
                authorizer=self._authorizer,
            )
            return self._local_data.exchange_client

    @property
    def auth_client(self) -> AuthClient:
        """A thread local copy of the Globus AuthClient."""
        if datetime.now() - self.login_time < timedelta(minutes=30):
            try:
                return self._local_data.auth_client
            except AttributeError:  # pragma: no cover
                pass

        logger.info('Initializing auth client.')

        if self._app is None:
            raise NotImplementedError(
                'Launching child agents is\
                currently not implemented.',
            )

        with self._app_lock:
            self._app.add_scope_requirements(
                {
                    AuthScopes.resource_server: [
                        AuthScopes.manage_projects,
                        AuthScopes.email,
                    ],
                },
            )

            self._app.login(
                force=True,
                auth_params=GlobusAuthorizationParameters(prompt='login'),
            )

            self.login_time = datetime.now()

        self._local_data.auth_client = AuthClient(
            app=self._app,
        )
        return self._local_data.auth_client

    def _register_client(self) -> None:
        self.exchange_client.register_client(self.mailbox_id)

    @classmethod
    async def new(
        cls,
        *,
        project_id: uuid.UUID,
        app: GlobusApp | None = None,
        authorizer: GlobusAuthorizer | None = None,
        mailbox_id: EntityId | None = None,
        name: str | None = None,
    ) -> Self:
        """Instantiate a new transport.

        Args:
            project_id: Globus Identifier of project to create agents under.
            app: For user authorization through token retrieval
            authorizer: For service authorization through token retrieval
            mailbox_id: Bind the transport to the specific mailbox. If `None`,
                a new user entity will be registered and the transport will be
                bound to that mailbox.
            name: Display name of the registered entity if `mailbox_id` is
                `None`.

        Returns:
            An instantiated transport bound to a specific mailbox.
        """
        loop = asyncio.get_running_loop()

        if mailbox_id is None:
            mailbox_id = UserId.new(name=name)
            client = cls(
                mailbox_id,
                project_id=project_id,
                app=app,
                authorizer=authorizer,
            )
            await loop.run_in_executor(
                client.executor,
                client._register_client,
            )
            logger.info('Registered %s in exchange', mailbox_id)
            return client

        return cls(
            mailbox_id,
            project_id=project_id,
            app=app,
            authorizer=authorizer,
        )

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    def _clean_up(self) -> None:
        for client_id in self.child_clients:
            self.auth_client.delete_client(client_id)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._clean_up)

    def _discover(
        self,
        agent: type[Agent],
        allow_subclasses: bool,
    ) -> GlobusHTTPResponse:
        return self.exchange_client.discover(
            agent,
            allow_subclasses=allow_subclasses,
        )

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            self.executor,
            self._discover,
            agent,
            allow_subclasses,
        )

        agent_ids_str = response['agent_ids']
        agent_ids = [aid for aid in agent_ids_str.split(',') if len(aid) > 0]
        return tuple(AgentId(uid=uuid.UUID(aid)) for aid in agent_ids)

    def factory(self) -> GlobusExchangeFactory:
        return GlobusExchangeFactory(self.project)

    def _recv(self, timeout: float | None) -> GlobusHTTPResponse:
        return self.exchange_client.recv(self.mailbox_id, timeout)

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        loop = asyncio.get_running_loop()
        try:
            try:
                logger.info(f'Receiving message for mailbox {self.mailbox_id}')
                response = await asyncio.wait_for(
                    loop.run_in_executor(
                        self.executor,
                        self._recv,
                        timeout,
                    ),
                    timeout,
                )
                message_raw = response['message']
                logger.info(f'Received message of length {len(message_raw)}')
            except AcademyAPIError as e:
                if e.http_status == StatusCode.TERMINATED.value:
                    raise MailboxTerminatedError(self.mailbox_id) from e
                elif (
                    e.http_status == StatusCode.TIMEOUT.value
                ):  # pragma: no cover
                    raise TimeoutError() from e
                raise e  # pragma: no cover
        except asyncio.TimeoutError as e:
            # In older versions of Python, ayncio.TimeoutError and TimeoutError
            # are different types.
            raise TimeoutError(
                f'Failed to receive response in {timeout} seconds.',
            ) from e

        return Message.model_validate_json(message_raw)

    def _create_registration(
        self,
        aid: AgentId[AgentT],
    ) -> GlobusAgentRegistration[AgentT]:
        # Create new resource server (auth entity)
        logger.info('Creating new auth client')
        client_response = self.auth_client.create_client(
            str(aid),
            project=self.project,
            client_type='hybrid_confidential_client_resource_server',
            visibility='private',
        )
        client_id = client_response['client']['id']
        logger.info(f'Created client id: {client_id}')

        # Create secret
        logger.info('Creating secret.')
        credentials_response = self.auth_client.create_client_credential(
            client_id,
            'Launch Credentials',
        )
        secret = credentials_response['credential']['secret']

        # Create scope
        logger.info('Creating new scope.')
        scope_response = self.auth_client.create_scope(
            client_id,
            'Agent launch',
            'Launch agent',
            'launch',
            dependent_scopes=[
                DependentScopeSpec(
                    ACADEMY_EXCHANGE_SCOPE_ID,
                    optional=False,
                    requires_refresh_token=True,
                ),
            ],
            allows_refresh_token=True,
        )
        scope = Scope.parse(scope_response['scopes'][0]['scope_string'])

        # Create delegated token
        logger.info('Creating delegated token.')
        assert self._app is not None  # Already true, but not caught by mypy
        self._app.add_scope_requirements(
            {client_id: [scope]},
        )
        authorizer = self._app.get_authorizer(client_id)
        bearer = authorizer.get_authorization_header()
        if bearer is None:  # pragma: no cover
            raise UnauthorizedError('Unable to get authorization headers.')

        auth_header_parts = bearer.split(' ')
        token = auth_header_parts[1]

        return GlobusAgentRegistration(
            agent_id=aid,
            client_id=client_id,
            token=token,
            secret=secret,
        )

    def _create_mailbox(
        self,
        aid: AgentId[AgentT],
        agent: type[AgentT],
    ) -> None:
        self.exchange_client.register_agent(aid, agent)

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> GlobusAgentRegistration[AgentT]:
        loop = asyncio.get_running_loop()

        # Create registration
        aid: AgentId[AgentT] = AgentId.new(name=name)
        registration = await loop.run_in_executor(
            self.executor,
            self._create_registration,
            aid,
        )

        # Create mailbox
        await loop.run_in_executor(
            self.executor,
            self._create_mailbox,
            aid,
            agent,
        )

        self.child_clients.append(registration.client_id)

        return registration

    def _send(self, message: Message[Any]) -> None:
        self.exchange_client.send(message)

    async def send(self, message: Message[Any]) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self.executor,
                self._send,
                message,
            )
        except AcademyAPIError as e:
            if e.http_status == StatusCode.NOT_FOUND.value:
                raise BadEntityIdError(message.dest) from e
            elif e.http_status == StatusCode.TERMINATED.value:
                raise MailboxTerminatedError(message.dest) from e
            raise e  # pragma: no cover

    def _status(self, uid: EntityId) -> MailboxStatus:
        response = self.exchange_client.status(uid)
        status = response['status']
        return MailboxStatus(status)

    async def status(self, uid: EntityId) -> MailboxStatus:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor,
            self._status,
            uid,
        )

    def _terminate(self, uid: EntityId) -> None:
        self.exchange_client.terminate(uid)

    async def terminate(self, uid: EntityId) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self.executor,
            self._terminate,
            uid,
        )


class GlobusExchangeFactory(ExchangeFactory[GlobusExchangeTransport]):
    """Globus exchange client factory.

    Args:
        project_id: Project to create new clients under. Must be able
            to authenticate as a administrator.
    """

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project = project_id

    async def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        registration: GlobusAgentRegistration[Any] | None = None,  # type: ignore[override]
    ) -> GlobusExchangeTransport:
        loop = asyncio.get_running_loop()

        if registration is None:
            app = await loop.run_in_executor(
                None,
                get_globus_app,
            )
            return await GlobusExchangeTransport.new(
                app=app,
                mailbox_id=mailbox_id,
                name=name,
                project_id=self.project,
            )
        else:
            logger.info('Initializing auth client for new agent.')
            auth_client = globus_sdk.ConfidentialAppAuthClient(
                client_id=registration.client_id,
                client_secret=registration.secret,
            )

            logger.info('Exchanging dependent token.')
            dependent_token_response = await loop.run_in_executor(
                None,
                functools.partial(
                    auth_client.oauth2_get_dependent_tokens,
                    registration.token,
                    refresh_tokens=True,
                    scope=AcademyExchangeScopes.academy_exchange,
                ),
            )

            # TODO: dependent token only provides access to exchange
            # So agents do not have access to auth. to create new agents.
            authorizer = globus_sdk.RefreshTokenAuthorizer(
                dependent_token_response.by_resource_server[
                    AcademyExchangeScopes.resource_server
                ]['refresh_token'],
                auth_client,
            )

            return await GlobusExchangeTransport.new(
                authorizer=authorizer,
                mailbox_id=mailbox_id,
                name=name,
                project_id=self.project,
            )
