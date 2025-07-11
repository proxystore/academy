from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import sys
import uuid
from collections.abc import Awaitable
from types import TracebackType
from typing import Any
from typing import Callable
from typing import Generic
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.agent import AgentT
from academy.context import ActionContext
from academy.context import AgentContext
from academy.exception import ActionCancelledError
from academy.exception import ExchangeError
from academy.exception import MailboxTerminatedError
from academy.exception import raise_exceptions
from academy.exchange import AgentExchangeClient
from academy.exchange import ExchangeFactory
from academy.exchange.transport import AgentRegistrationT
from academy.exchange.transport import ExchangeTransportT
from academy.handle import Handle
from academy.handle import HandleDict
from academy.handle import HandleList
from academy.handle import ProxyHandle
from academy.handle import RemoteHandle
from academy.handle import UnboundRemoteHandle
from academy.identifier import EntityId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import PingRequest
from academy.message import Request
from academy.message import Response
from academy.message import ResponseT_co
from academy.message import ShutdownRequest
from academy.message import SuccessResponse
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclasses.dataclass
class _ShutdownState:
    # If the shutdown was expected or due to an error
    expected_shutdown: bool = True
    # Override the termination setting of the run config
    terminate_override: bool | None = None


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    """Agent runtime configuration.

    Attributes:
        cancel_actions_on_shutdown: Cancel running actions when the agent
            is shutdown, otherwise wait for the actions to finish.
        raise_loop_errors_on_shutdown: Raise any captured loop errors when
            the agent is shutdown.
        shutdown_on_loop_error: Shutdown the agent if any loop raises an error.
        terminate_on_error: Terminate the agent by closing its mailbox
            permanently if the agent shuts down due to an error.
        terminate_on_success: Terminate the agent by closing its mailbox
            permanently if the agent shuts down without an error.
    """

    cancel_actions_on_shutdown: bool = True
    raise_loop_errors_on_shutdown: bool = True
    shutdown_on_loop_error: bool = True
    terminate_on_error: bool = True
    terminate_on_success: bool = True


class Runtime(Generic[AgentT], NoPickleMixin):
    """Agent runtime manager.

    The runtime is used to execute an agent by managing stateful resources,
    startup/shutdown, lifecycle hooks, and concurrency.

    An agent can be run in two ways:
    ```python
    runtime = Runtime(agent, ...)

    # Option 1: Async context manager
    async with runtime:
        ...
        await runtime.wait_shutdown()

    # Option 2: Run until complete
    await runtime.run_until_complete()
    ```

    Note:
        A runtime can only be used once, after which attempts to run an
        agent using the same runtime with raise a
        [`RuntimeError`][RuntimeError].

    Note:
        If any `@loop` method raises an error, the agent will be signaled
        to shutdown if `shutdown_on_loop_error` is set in the `config`.

    Args:
        agent: Agent that the agent will exhibit.
        exchange_factory: Message exchange factory.
        registration: Agent registration info returned by the exchange.
        config: Agent execution parameters.
    """

    def __init__(
        self,
        agent: AgentT,
        *,
        exchange_factory: ExchangeFactory[ExchangeTransportT],
        registration: AgentRegistrationT,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.agent_id = registration.agent_id
        self.agent = agent
        self.factory = exchange_factory
        self.registration = registration
        self.config = config if config is not None else RuntimeConfig()

        self._actions = agent._agent_actions()
        self._loops = agent._agent_loops()

        self._started_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._shutdown_options = _ShutdownState()
        self._agent_startup_called = False

        self._action_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._loop_tasks: dict[str, asyncio.Task[None]] = {}
        self._loop_exceptions: list[tuple[str, Exception]] = []

        self._exchange_client: (
            AgentExchangeClient[AgentT, ExchangeTransportT] | None
        ) = None
        self._exchange_listener_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        try:
            await self._start()
        except:
            self.signal_shutdown(expected=False)
            await self._shutdown()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self._shutdown()

    def __repr__(self) -> str:
        name = type(self).__name__
        return f'{name}({self.agent!r}, {self._exchange_client!r})'

    def __str__(self) -> str:
        name = type(self).__name__
        agent = type(self.agent).__name__
        return f'{name}<{agent}; {self.agent_id}>'

    async def _send_response(self, response: Message[ResponseT_co]) -> None:
        assert self._exchange_client is not None
        try:
            await self._exchange_client.send(response)
        except MailboxTerminatedError:  # pragma: no cover
            logger.warning(
                'Failed to send response from %s to %s because the '
                'destination mailbox was terminated.',
                self.agent_id,
                response.dest,
            )
        except ExchangeError:  # pragma: no cover
            logger.exception(
                'Failed to send response from %s to %s.',
                self.agent_id,
                response.dest,
            )

    async def _execute_action(self, request: Message[ActionRequest]) -> None:
        body = request.get_body()
        response: Message[Response]
        try:
            result = await self.action(
                body.action,
                request.src,
                args=body.get_args(),
                kwargs=body.get_kwargs(),
            )
        except asyncio.CancelledError:
            response = request.create_response(
                ErrorResponse(exception=ActionCancelledError(body.action)),
            )
        except Exception as e:
            response = request.create_response(ErrorResponse(exception=e))
        else:
            response = request.create_response(
                ActionResponse(action=body.action, result=result),
            )
        finally:
            # Shield sending the result from being cancelled so the requester
            # does not block on a response they will never get.
            await asyncio.shield(self._send_response(response))

    async def _execute_loop(
        self,
        name: str,
        method: Callable[[asyncio.Event], Awaitable[None]],
    ) -> None:
        try:
            await method(self._shutdown_event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._loop_exceptions.append((name, e))
            logger.exception(
                'Error in loop %r (signaling shutdown: %s)',
                name,
                self.config.shutdown_on_loop_error,
            )
            if self.config.shutdown_on_loop_error:
                self.signal_shutdown(expected=False)

    async def _request_handler(self, request: Message[Request]) -> None:
        body = request.get_body()
        if isinstance(body, ActionRequest):
            task = asyncio.create_task(
                self._execute_action(request),  # type: ignore[arg-type]
                name=f'execute-action-{body.action}-{request.tag}',
            )
            self._action_tasks[request.tag] = task
            task.add_done_callback(
                lambda _: self._action_tasks.pop(request.tag),
            )
        elif isinstance(body, PingRequest):
            logger.info('Ping request received by %s', self.agent_id)
            response = request.create_response(SuccessResponse())
            await self._send_response(response)
        elif isinstance(body, ShutdownRequest):
            self.signal_shutdown(expected=True, terminate=body.terminate)
        else:
            raise AssertionError('Unreachable.')

    async def action(
        self,
        action: str,
        source_id: EntityId,
        *,
        args: Any,
        kwargs: Any,
    ) -> Any:
        """Invoke an action of the agent's agent.

        Args:
            action: Name of action to invoke.
            source_id: ID of the source that requested the action.
            args: Tuple of positional arguments.
            kwargs: Dictionary of keyword arguments.

        Returns:
            Result of the action.

        Raises:
            AttributeError: If an action with this name is not implemented by
                the agent's agent.
        """
        logger.debug('Invoking "%s" action on %s', action, self.agent_id)
        if action not in self._actions:
            raise AttributeError(
                f'{self.agent} does not have an action named "{action}".',
            )
        action_method = self._actions[action]
        if action_method._action_method_context:
            assert self._exchange_client is not None
            context = ActionContext(source_id, self._exchange_client)
            return await action_method(*args, context=context, **kwargs)
        else:
            return await action_method(*args, **kwargs)

    async def run_until_complete(self) -> None:
        """Run the agent until shutdown.

        Agent startup involves:

        1. Creates a new exchange client for the agent.
        1. Sets the runtime context on the agent.
        1. Binds all handles of the agent to this agent's exchange client.
        1. Starts a [`Task`][asyncio.Task] to listen for messages in the
           agent's mailbox in the exchange.
        1. Starts a [`Task`][asyncio.Task] for all control loops defined on
           the agent.
        1. Calls
           [`Agent.agent_on_startup()`][academy.agent.Agent.agent_on_startup].

        After startup succeeds, this method waits for the agent to be shutdown,
        such as due to a failure in a control loop or receiving a shutdown
        message.

        Agent shutdown involves:

        1. Calls
           [`Agent.agent_on_shutdown()`][academy.agent.Agent.agent_on_shutdown].
        1. Cancels running control loop tasks.
        1. Cancels the mailbox message listener task so no new requests are
           received.
        1. Waits for any currently executing actions to complete.
        1. Terminates the agent's mailbox in the exchange if configured.
        1. Closes the exchange client.

        Raises:
            RuntimeError: If the agent has already been shutdown.
            Exception: Any exceptions raised during startup, shutdown, or
                inside of control loops.
        """
        async with self:
            await self.wait_shutdown()

    async def _start(self) -> None:
        if self._shutdown_event.is_set():
            raise RuntimeError('Agent has already been shutdown.')

        logger.debug(
            'Starting agent... (%s; %s)',
            self.agent_id,
            self.agent,
        )

        self._exchange_client = await self.factory.create_agent_client(
            self.registration,
            request_handler=self._request_handler,
        )

        context = AgentContext(
            agent_id=self.agent_id,
            exchange_client=self._exchange_client,
            shutdown_event=self._shutdown_event,
        )
        self.agent._agent_set_context(context)
        _bind_agent_handles(self.agent, self._exchange_client)

        self._exchange_listener_task = asyncio.create_task(
            self._exchange_client._listen_for_messages(),
            name=f'exchange-listener-{self.agent_id}',
        )

        for name, method in self._loops.items():
            task = asyncio.create_task(
                self._execute_loop(name, method),
                name=f'execute-loop-{name}-{self.agent_id}',
            )
            self._loop_tasks[name] = task

        await self.agent.agent_on_startup()
        self._agent_startup_called = True

        self._started_event.set()
        logger.info('Running agent (%s; %s)', self.agent_id, self.agent)

    def _should_terminate_mailbox(self) -> bool:
        # Inspects the shutdown options and the run config to determine
        # if the agent's mailbox should be terminated in the exchange.
        if self._shutdown_options.terminate_override is not None:
            return self._shutdown_options.terminate_override

        expected = self._shutdown_options.expected_shutdown
        terminate_for_success = self.config.terminate_on_success and expected
        terminate_for_error = self.config.terminate_on_error and not expected
        return terminate_for_success or terminate_for_error

    async def _shutdown(self) -> None:
        logger.debug(
            'Shutting down agent... (expected: %s; %s; %s)',
            self._shutdown_options.expected_shutdown,
            self.agent_id,
            self.agent,
        )

        self._shutdown_event.set()

        if self._agent_startup_called:
            # Don't call agent_on_shutdown() if we never called
            # agent_on_startup()
            await self.agent.agent_on_shutdown()

        # Cancel running control loop tasks
        for task in self._loop_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # If _start() fails early, the listener task may not have started.
        if self._exchange_listener_task is not None:
            # Stop exchange listener thread before cancelling waiting on
            # running actions so we know that we won't receive an new
            # action requests
            self._exchange_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._exchange_listener_task

        # Wait for running actions to complete
        for task in tuple(self._action_tasks.values()):
            # Both branches should be covered by
            # test_agent_action_message_cancelled but a slow test runner could
            # not begin shutdown until all the tasks have completed anyways
            if self.config.cancel_actions_on_shutdown:  # pragma: no branch
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self._exchange_client is not None:
            if self._should_terminate_mailbox():
                await self._exchange_client.terminate(self.agent_id)
            await self._exchange_client.close()

        if self.config.raise_loop_errors_on_shutdown:
            # Raise loop exceptions so the caller sees them, even if the loop
            # errors didn't cause the shutdown.
            raise_exceptions(
                (e for _, e in self._loop_exceptions),
                message='Caught failures in agent loops while shutting down.',
            )

        logger.info('Shutdown agent (%s; %s)', self.agent_id, self.agent)

    def signal_shutdown(
        self,
        *,
        expected: bool = True,
        terminate: bool | None = None,
    ) -> None:
        """Signal that the agent should exit.

        If the agent has not started, this will cause the agent to immediately
        shutdown when next started. If the agent is shutdown, this has no
        effect.

        Args:
            expected: If the reason for the shutdown was due to normal
                expected reasons or due to unexpected errors.
            terminate: Optionally override the mailbox termination settings
                in the run config.
        """
        self._shutdown_options = _ShutdownState(
            expected_shutdown=expected,
            terminate_override=terminate,
        )
        self._shutdown_event.set()

    async def wait_shutdown(self, timeout: float | None = None) -> None:
        """Wait for agent shutdown to be signalled.

        Args:
            timeout: Optional timeout in seconds to wait for a shutdown event.

        Raises:
            TimeoutError: If `timeout` was exceeded while waiting for agents.
        """
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # In Python 3.10 and older, asyncio.TimeoutError and TimeoutError
            # are different error types.
            raise TimeoutError(
                f'Agent shutdown was not signalled within {timeout} seconds.',
            ) from None


def _bind_agent_handles(
    agent: AgentT,
    client: AgentExchangeClient[AgentT, Any],
) -> None:
    """Bind all handle instance attributes on a agent.

    Warning:
        This mutates the agent, replacing the attributes with new handles
        bound to the agent's exchange client.

    Args:
        agent: The agent to bind handles on.
        client: The agent's exchange client used to bind the handles.
    """

    def _bind(handle: Handle[AgentT]) -> Handle[AgentT]:
        if isinstance(handle, ProxyHandle):
            return handle
        if (
            isinstance(handle, RemoteHandle)
            and handle.client_id == client.client_id
        ):
            return handle

        assert isinstance(handle, (UnboundRemoteHandle, RemoteHandle))
        bound = client.get_handle(handle.agent_id)
        logger.debug(
            'Bound %s of %s to %s',
            handle,
            agent,
            client.client_id,
        )
        return bound

    for attr, handles in agent._agent_handles().items():
        if isinstance(handles, HandleDict):
            bound_dict = HandleDict(
                {k: _bind(h) for k, h in handles.items()},
            )
            setattr(agent, attr, bound_dict)
        elif isinstance(handles, HandleList):
            bound_list = HandleList([_bind(h) for h in handles])
            setattr(agent, attr, bound_list)
        else:
            setattr(agent, attr, _bind(handles))
