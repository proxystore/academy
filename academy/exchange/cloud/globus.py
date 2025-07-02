# ruff: noqa: D102
from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import sys
import uuid
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

from academy.agent import AgentT
from academy.behavior import Behavior
from academy.behavior import BehaviorT
from academy.exception import BadEntityIdError
from academy.exception import MailboxTerminatedError
from academy.exchange import ExchangeFactory
from academy.exchange.cloud.login import get_globus_app
from academy.exchange.cloud.scopes import AcademyExchangeScopes
from academy.exchange.cloud.server import _FORBIDDEN_CODE
from academy.exchange.cloud.server import _NOT_FOUND_CODE
from academy.exchange.cloud.server import _TIMEOUT_CODE
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
    base_url = 'https://exchange.academy.globus.org'  # TODO: Get domain
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
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> GlobusHTTPResponse:
        behavior_str = f'{behavior.__module__}.{behavior.__name__}'
        return self.request(
            'GET',
            self._discover_url,
            data={
                'behavior': behavior_str,
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
        agent_id: AgentId[BehaviorT],
        behavior: type[BehaviorT],
    ) -> GlobusHTTPResponse:
        return self.post(
            self._mailbox_url,
            data={
                'mailbox': agent_id.model_dump_json(),
                'behavior': ','.join(behavior.behavior_mro()),
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
        app: For user authorization through token retrieval
        authorizer: For service authorization through token retrieval
    """

    def __init__(
        self,
        mailbox_id: EntityId,
        *,
        app: GlobusApp | None = None,
        authorizer: GlobusAuthorizer | None,
    ) -> None:
        self._mailbox_id = mailbox_id
        self._app = app
        self._authorizer = authorizer

        self._client = AcademyGlobusClient(app=app, authorizer=authorizer)

        # Lazily initialize auth client so auth scopes are not always required.
        self._auth_client: AuthClient | None = None

    @classmethod
    async def new(
        cls,
        *,
        app: GlobusApp | None = None,
        authorizer: GlobusAuthorizer | None = None,
        mailbox_id: EntityId | None = None,
        name: str | None = None,
    ) -> Self:
        """Instantiate a new transport.

        Args:
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
            globus_client = AcademyGlobusClient(app=app, authorizer=authorizer)
            mailbox_id = UserId.new(name=name)
            await loop.run_in_executor(
                None,
                globus_client.register_client,
                mailbox_id,
            )
            logger.info('Registered %s in exchange', mailbox_id)

        return cls(mailbox_id, app=app, authorizer=authorizer)

    @property
    def mailbox_id(self) -> EntityId:
        return self._mailbox_id

    async def close(self) -> None:
        return

    async def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            functools.partial(
                self._client.discover,
                behavior,
                allow_subclasses=allow_subclasses,
            ),
        )

        agent_ids_str = response['agent_ids']
        agent_ids = [aid for aid in agent_ids_str.split(',') if len(aid) > 0]
        return tuple(AgentId(uid=uuid.UUID(aid)) for aid in agent_ids)

    def factory(self) -> GlobusExchangeFactory:
        return GlobusExchangeFactory()

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                try:
                    response = await loop.run_in_executor(
                        None,
                        self._client.recv,
                        self.mailbox_id,
                    )
                    message_raw = response['message']
                except AcademyAPIError as e:
                    if e.http_status == _FORBIDDEN_CODE:
                        raise MailboxTerminatedError(self.mailbox_id) from e
                    elif e.http_status == _TIMEOUT_CODE:
                        raise TimeoutError() from e
                    raise e
        except asyncio.TimeoutError as e:
            # In older versions of Python, ayncio.TimeoutError and TimeoutError
            # are different types.
            raise TimeoutError(
                f'Failed to receive response in {timeout} seconds.',
            ) from e

        return Message.model_validate_json(message_raw)

    async def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
    ) -> GlobusAgentRegistration[BehaviorT]:
        if self._app is None:
            raise NotImplementedError(
                'Launching child agents is\
                                       currently not implemented.',
            )

        loop = asyncio.get_running_loop()
        if self._auth_client is None:
            authorizer = self._authorizer
            if authorizer is None:
                assert isinstance(globus_sdk.AuthClient.resource_server, str)
                loop.run_in_executor(
                    None,
                    self._app.get_authorizer,
                    globus_sdk.AuthClient.resource_server,
                )

            # TODO: Should we create a client as the user or as the service?
            self._auth_client = AuthClient(
                authorizer=authorizer,
            )

        # Create client id
        aid: AgentId[BehaviorT] = AgentId.new(name=name)
        client_response = await loop.run_in_executor(
            None,
            functools.partial(
                self._auth_client.create_client,
                str(aid),
                project=ACADEMY_PROJECT,
                client_type='hybrid_confidential_client_resource_server',
                visibility='private',
            ),
        )
        client_id = client_response['client']['id']

        # Create secret
        credentials_response = await loop.run_in_executor(
            None,
            self._auth_client.create_client_credential,
            client_id,
            'Launch Credentials',
        )
        secret = credentials_response['credential']['secret']

        # Create scope
        scope_response = await loop.run_in_executor(
            None,
            functools.partial(
                self._auth_client.create_scope,
                client_id,
                'Agent launch',
                'Launch agent',
                'launch',
                dependent_scopes=[
                    DependentScopeSpec(
                        AcademyExchangeScopes.academy_exchange,
                        optional=False,
                        requires_refresh_token=True,
                    ),
                ],
                allows_refresh_token=True,
            ),
        )
        scope = Scope.parse(scope_response['scope']['scope_string'])

        # Create delegated token
        self._app.add_scope_requirements(
            {client_id: [scope]},
        )
        authorizer = await loop.run_in_executor(
            None,
            self._app.get_authorizer,
            client_id,
        )

        bearer = await loop.run_in_executor(
            None,
            authorizer.get_authorization_header,
        )
        assert bearer is not None, 'Unable to get authorization headers.'

        # Create mailbox
        await loop.run_in_executor(
            None,
            self._client.register_agent,
            aid,
            behavior,
        )
        return GlobusAgentRegistration(
            agent_id=aid,
            client_id=client_id,
            token=bearer,
            secret=secret,
        )

    async def send(self, message: Message[Any]) -> None:
        loop = asyncio.get_running_loop()

        try:
            await loop.run_in_executor(
                None,
                self._client.send,
                message,
            )
        except AcademyAPIError as e:
            if e.http_status == _NOT_FOUND_CODE:
                raise BadEntityIdError(message.dest) from e
            elif e.http_status == _FORBIDDEN_CODE:
                raise MailboxTerminatedError(message.dest) from e
            raise e

    async def status(self, uid: EntityId) -> MailboxStatus:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            self._client.status,
            uid,
        )

        status = response['status']
        return MailboxStatus(status)

    async def terminate(self, uid: EntityId) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._client.terminate,
            uid,
        )


class GlobusExchangeFactory(ExchangeFactory[GlobusExchangeTransport]):
    """Globus exchange client factory."""

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
            )
        else:
            auth_client = globus_sdk.ConfidentialAppAuthClient(
                client_id=registration.client_id,
                client_secret=registration.secret,
            )
            dependent_token_response = await loop.run_in_executor(
                None,
                functools.partial(
                    auth_client.oauth2_get_dependent_tokens,
                    registration.token,
                    refresh_tokens=True,
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
            )
