from __future__ import annotations

import dataclasses
import enum
import logging
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from typing import Generic
from typing import TypeVar

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.handle import Handle
from academy.handle import ProxyHandle
from academy.handle import UnboundRemoteHandle
from academy.identifier import AgentId
from academy.message import ActionRequest
from academy.message import PingRequest
from academy.message import RequestMessage
from academy.message import ResponseMessage
from academy.message import ShutdownRequest

logger = logging.getLogger(__name__)

T = TypeVar('T')
BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class _AgentState(enum.Enum):
    INITIALIZED = 'initialized'
    STARTING = 'starting'
    RUNNING = 'running'
    TERMINTATING = 'terminating'
    SHUTDOWN = 'shutdown'


@dataclasses.dataclass(frozen=True)
class AgentRunConfig:
    """Agent run configuration.

    Attributes:
        close_exchange_on_exit: Close the exchange interface when the agent
            exits. Typically this should be `True` to clean up resources,
            except when multiple agents are running in the same process
            and sharing an exchange.
        max_action_concurrency: Maximum size of the thread pool used to
            concurrently execute action requests.
        terminate_on_error: Terminate the agent by closing its mailbox
            permanently if the agent fails.
        terminate_on_exit: Terminate the agent by closing its mailbox
            permanently after the agent exits.
    """

    close_exchange_on_exit: bool = True
    max_action_concurrency: int | None = None
    terminate_on_error: bool = True
    terminate_on_exit: bool = True


# Helper for Agent.__reduce__ which cannot handle the keyword arguments
# of the Agent constructor.
def _agent_trampoline(
    behavior: BehaviorT,
    agent_id: AgentId[BehaviorT],
    exchange: ExchangeFactory,
    config: AgentRunConfig,
) -> Agent[BehaviorT]:
    return Agent(
        behavior,
        agent_id=agent_id,
        exchange=exchange,
        config=config,
    )


class Agent(Generic[BehaviorT]):
    """Executable agent.

    An agent executes predefined [`Behavior`][academy.behavior.Behavior]. An
    agent can operate independently or as part of a broader multi-agent
    system.

    Note:
        An agent can only be run once. After `shutdown()` is called, later
        operations will raise a `RuntimeError`.

    Note:
        If any `@loop` method raises an error, the agent will be signaled
        to shutdown.

    Args:
        behavior: Behavior that the agent will exhibit.
        agent_id: EntityId of this agent in a multi-agent system.
        exchange: Message exchange of multi-agent system. The agent will close
            the exchange when it finished running.
        config: Agent execution parameters.
    """

    def __init__(
        self,
        behavior: BehaviorT,
        *,
        agent_id: AgentId[BehaviorT],
        exchange: ExchangeFactory,
        config: AgentRunConfig | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.behavior = behavior
        self.exchange = exchange.create_agent_client(
            agent_id,
            request_handler=self._request_handler,
        )
        self.config = config if config is not None else AgentRunConfig()

        self._actions = behavior.behavior_actions()
        self._loops = behavior.behavior_loops()

        self._shutdown = threading.Event()
        self._expected_shutdown = False
        self._state_lock = threading.Lock()
        self._state = _AgentState.INITIALIZED

        self._action_pool: ThreadPoolExecutor | None = None
        self._action_futures: dict[ActionRequest, Future[None]] = {}
        self._loop_pool: ThreadPoolExecutor | None = None
        self._loop_futures: dict[Future[None], str] = {}

    def __call__(self) -> None:
        """Alias for [run()][academy.agent.Agent.run]."""
        self.run()

    def __repr__(self) -> str:
        name = type(self).__name__
        return (
            f'{name}(agent_id={self.agent_id!r}, behavior={self.behavior!r}, '
            f'exchange={self.exchange!r})'
        )

    def __str__(self) -> str:
        name = type(self).__name__
        behavior = type(self.behavior).__name__
        return f'{name}<{behavior}; {self.agent_id}>'

    def __reduce__(self) -> Any:
        return (
            _agent_trampoline,
            (
                self.behavior,
                self.agent_id,
                self.exchange.factory(),
                self.config,
            ),
        )

    def _bind_handle(self, handle: Handle[BehaviorT]) -> Handle[BehaviorT]:
        if isinstance(handle, ProxyHandle):  # pragma: no cover
            return handle
        if (
            isinstance(handle, BoundRemoteHandle)
            and handle.mailbox_id == self.agent_id
        ):
            return handle

        assert isinstance(handle, (UnboundRemoteHandle, BoundRemoteHandle))
        bound = handle.bind_to_exchange(self.exchange)
        logger.debug(
            'Bound handle to %s to running agent with %s',
            handle.agent_id,
            self.agent_id,
        )
        return bound

    def _bind_handles(self) -> None:
        self.behavior.behavior_handles_bind(self._bind_handle)

    def _send_response(self, response: ResponseMessage) -> None:
        try:
            self.exchange.send(response.dest, response)
        except (BadEntityIdError, MailboxClosedError):
            logger.warning(
                'Failed to send response from %s to %s. '
                'This likely means the destination mailbox was '
                'removed from the exchange.',
                self.agent_id,
                response.dest,
            )

    def _execute_action(self, request: ActionRequest) -> None:
        try:
            result = self.action(request.action, request.pargs, request.kargs)
        except Exception as e:
            response = request.error(exception=e)
        else:
            response = request.response(result=result)
        self._send_response(response)

    def _request_handler(self, request: RequestMessage) -> None:
        if isinstance(request, ActionRequest):
            # The _request_handler should only be called within the message
            # handler thread which is only started after the _action_pool
            # is initialized.
            assert self._action_pool is not None
            future = self._action_pool.submit(self._execute_action, request)
            self._action_futures[request] = future
            future.add_done_callback(
                lambda _: self._action_futures.pop(request),
            )
        elif isinstance(request, PingRequest):
            logger.info('Ping request received by %s', self.agent_id)
            self._send_response(request.response())
        elif isinstance(request, ShutdownRequest):
            self.signal_shutdown()
        else:
            raise AssertionError('Unreachable.')

    def _loop_callback(self, future: Future[None]) -> None:
        if future.exception() is not None:
            name = self._loop_futures[future]
            logger.warning(
                'Error in loop %r (signaling shutdown): %r',
                name,
                future.exception(),
            )
            self.signal_shutdown(expected=False)

    def action(self, action: str, args: Any, kwargs: Any) -> Any:
        """Invoke an action of the agent.

        Args:
            action: Name of action to invoke.
            args: Tuple of positional arguments.
            kwargs: Dictionary of keyword arguments.

        Returns:
            Result of the action.

        Raises:
            AttributeError: if an action with this name is not implemented by
                the behavior of the agent.
        """
        logger.debug('Invoking "%s" action on %s', action, self.agent_id)
        if action not in self._actions:
            raise AttributeError(
                f'Agent[{type(self.behavior).__name__}] does not have an '
                f'action named "{action}".',
            )
        return self._actions[action](*args, **kwargs)

    def run(self) -> None:
        """Run the agent.

        Starts the agent, waits for another thread to call `signal_shutdown()`,
        and then shuts down the agent.

        Raises:
            Exception: Any exceptions raised inside threads.
        """
        try:
            self.start()
            self._shutdown.wait()
        finally:
            self.shutdown()

    def start(self) -> None:
        """Start the agent.

        Note:
            This method is idempotent; it will return if the agent is
            already running. However, it will raise an error if the agent
            is shutdown.

        1. Binds all unbound handles to remote agents to this agent.
        1. Calls [`Behavior.on_setup()`][academy.behavior.Behavior.on_setup].
        1. Starts threads for all control loops defined on the agent's
           [`Behavior`][academy.behavior.Behavior].
        1. Starts a thread for listening to messages from the exchange.

        Raises:
            RuntimeError: If the agent has been shutdown.
        """
        with self._state_lock:
            if self._state is _AgentState.SHUTDOWN:
                raise RuntimeError('Agent has already been shutdown.')
            elif self._state is _AgentState.RUNNING:
                return

            logger.debug(
                'Starting agent... (%s; %s)',
                self.agent_id,
                self.behavior,
            )
            self._state = _AgentState.STARTING
            self._bind_handles()
            self.behavior.on_setup()
            self._action_pool = ThreadPoolExecutor(
                self.config.max_action_concurrency,
            )
            self._loop_pool = ThreadPoolExecutor(
                max_workers=len(self._loops) + 1,
            )

            for name, method in self._loops.items():
                loop_future = self._loop_pool.submit(method, self._shutdown)
                self._loop_futures[loop_future] = name
                loop_future.add_done_callback(self._loop_callback)

            listener_future = self._loop_pool.submit(
                self.exchange._listen_for_messages,
            )
            self._loop_futures[listener_future] = '_exchange.listen'

            self._state = _AgentState.RUNNING

            logger.info('Running agent (%s; %s)', self.agent_id, self.behavior)

    def shutdown(self) -> None:
        """Shutdown the agent.

        Note:
            This method is idempotent.

        1. Sets the shutdown [`Event`][threading.Event] passed to all control
           loops.
        1. Waits for any currently executing actions to complete.
        1. Closes the agent's mailbox indicating that no further messages
           will be processed.
        1. Waits for the control loop and message listener threads to exit.
        1. Optionally closes the exchange.
        1. Calls
           [`Behavior.on_shutdown()`][academy.behavior.Behavior.on_shutdown].

        Raises:
            Exception: Any exceptions raised inside threads.
        """
        with self._state_lock:
            if self._state is _AgentState.SHUTDOWN:
                return

            logger.debug(
                'Shutting down agent... (expected: %s; %s; %s)',
                self._expected_shutdown,
                self.agent_id,
                self.behavior,
            )
            self._state = _AgentState.TERMINTATING
            self._shutdown.set()

            # Cause the exchange message listener thread to exit by closing
            # the mailbox the exchange is listening to. This is done
            # first so we stop receiving new requests.
            self.exchange.terminate(self.agent_id)
            for future, name in self._loop_futures.items():
                if name == '_exchange.listen':
                    future.result()

            # Wait for currently running actions to complete. No more
            # should come in now that exchange's listener thread is done.
            if self._action_pool is not None:
                self._action_pool.shutdown(wait=True, cancel_futures=True)

            # Shutdown the loop pool after waiting on the loops to exit.
            if self._loop_pool is not None:
                self._loop_pool.shutdown(wait=True)

            if (
                self._expected_shutdown and not self.config.terminate_on_exit
            ) or (
                not self._expected_shutdown
                and not self.config.terminate_on_error
            ):
                # TODO: This is a hack because we need to close the mailbox
                # for the exchange listener thread to exit, but in some
                # cases we don't actually want to close it permanently. This
                # means there is a race where the mailbox is temporarily
                # closed.
                self.exchange.register_agent(
                    type(self.behavior),
                    _agent_id=self.agent_id,
                )

            self.behavior.on_shutdown()

            # Close the exchange last since the actions that finished
            # up may still need to use it to send replies.
            if self.config.close_exchange_on_exit:
                self.exchange.close()

            self._state = _AgentState.SHUTDOWN

            # Raise any exceptions from the loop threads as the final step.
            _raise_future_exceptions(tuple(self._loop_futures))

            logger.info(
                'Shutdown agent (%s; %s)',
                self.agent_id,
                self.behavior,
            )

    def signal_shutdown(self, expected: bool = True) -> None:
        """Signal that the agent should exit.

        If the agent has not started, this will cause the agent to immediately
        shutdown when next started. If the agent is shutdown, this has no
        effect.
        """
        self._expected_shutdown = expected
        self._shutdown.set()


def _raise_future_exceptions(futures: Sequence[Future[Any]]) -> None:
    if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
        exceptions: list[Exception] = []
        for future in futures:
            exception = future.exception()
            if isinstance(exception, Exception):
                exceptions.append(exception)
        if len(exceptions) > 0:
            raise ExceptionGroup(  # noqa: F821
                'Caught failures in agent loops while shutting down.',
                exceptions,
            )
    else:  # pragma: <3.11 cover
        for future in futures:
            try:
                future.result()
            except Exception as e:
                raise RuntimeError(
                    'Caught at least one failure in agent loops '
                    'while shutting down.',
                ) from e
