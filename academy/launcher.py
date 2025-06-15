from __future__ import annotations

import dataclasses
import logging
import sys
import threading
from concurrent.futures import CancelledError
from concurrent.futures import Executor
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import Any
from typing import Generic
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.agent import Agent
from academy.agent import AgentRunConfig
from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.identifier import AgentId

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


def _run_agent_on_worker(agent: Agent[Any]) -> None:
    # Agent implements __call__ so we could submit the agent directly
    # to Executor.submit() as the function to run. However, some executors
    # serialize code differently from arguments so avoid that we add
    # a level of indirection so the agent is an argument.
    agent.run()


@dataclasses.dataclass
class _ACB(Generic[BehaviorT]):
    # Agent Control Block
    agent_id: AgentId[BehaviorT]
    behavior: BehaviorT
    exchange: ExchangeFactory
    done: threading.Event
    future: Future[None] | None = None
    launch_count: int = 0


class Launcher:
    """Launcher that wraps a [`concurrent.futures.Executor`][concurrent.futures.Executor].

    Args:
        executor: Executor used for launching agents. Note that this class
            takes ownership of the `executor`.
        close_exchange: Passed along to the [`Agent`][academy.agent.Agent]
            constructor. This should typically be `True`, the default,
            when the executor runs agents in separate processes, but should
            be `False` for the `ThreadPoolExecutor` to avoid closing
            shared exchange objects.
        max_restarts: Maximum times to restart an agent if it exits with
            an error.
    """  # noqa: E501

    def __init__(
        self,
        executor: Executor,
        *,
        close_exchange: bool = True,
        max_restarts: int = 0,
    ) -> None:
        self._executor = executor
        self._close_exchange = close_exchange
        self._max_restarts = max_restarts
        self._acbs: dict[AgentId[Any], _ACB[Any]] = {}
        self._future_to_acb: dict[Future[None], _ACB[Any]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f'{type(self).__name__}(executor={self._executor!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{type(self._executor).__name__}>'

    def _callback(self, future: Future[None]) -> None:
        acb = self._future_to_acb.pop(future)
        done = True

        try:
            future.result()
            logger.debug('Completed agent future (%s)', acb.agent_id)
        except CancelledError:  # pragma: no cover
            logger.warning('Cancelled agent future (%s)', acb.agent_id)
        except Exception:  # pragma: no cover
            logger.exception('Received agent exception (%s)', acb.agent_id)
            if acb.launch_count < self._max_restarts + 1:
                self._launch(acb.agent_id)
                done = False

        if done:
            acb.done.set()

    def close(self) -> None:
        """Close the launcher.

        Warning:
            This will not return until all agents have exited. It is the
            caller's responsibility to shutdown agents prior to closing
            the launcher.
        """
        logger.debug('Waiting for agents to shutdown...')
        for acb in self._acbs.values():
            if acb.done.is_set() and acb.future is not None:
                # Raise possible errors from agents so user is sure
                # to see them.
                acb.future.result()
        self._executor.shutdown(wait=True, cancel_futures=True)
        logger.debug('Closed launcher (%s)', self)

    def _launch(self, agent_id: AgentId[Any]) -> None:
        acb = self._acbs[agent_id]

        agent = Agent(
            acb.behavior,
            agent_id=acb.agent_id,
            exchange=acb.exchange,
            config=AgentRunConfig(
                terminate_on_error=acb.launch_count + 1 >= self._max_restarts,
            ),
        )
        future = self._executor.submit(_run_agent_on_worker, agent)
        acb.launch_count += 1
        acb.future = future
        self._future_to_acb[future] = acb
        future.add_done_callback(self._callback)

        if acb.launch_count == 1:
            logger.debug(
                'Launched agent (%s; %s)',
                acb.agent_id,
                acb.behavior,
            )
        else:
            restarts = acb.launch_count - 1
            logger.debug(
                'Restarted agent (%d/%d retries; %s; %s)',
                restarts,
                self._max_restarts,
                acb.agent_id,
                acb.behavior,
            )

    def launch(
        self,
        behavior: BehaviorT,
        exchange: ExchangeClient,
        *,
        agent_id: AgentId[BehaviorT] | None = None,
        name: str | None = None,
    ) -> BoundRemoteHandle[BehaviorT]:
        """Launch a new agent with a specified behavior.

        Args:
            behavior: Behavior the agent should implement.
            exchange: Exchange the agent will use for messaging.
            agent_id: Specify ID of the launched agent. If `None`, a new
                agent ID will be created within the exchange.
            name: Readable name of the agent. Ignored if `agent_id` is
                provided.

        Returns:
            Handle (unbound) used to interact with the agent.
        """
        agent_id = (
            exchange.register_agent(type(behavior), name=name)
            if agent_id is None
            else agent_id
        )

        acb = _ACB(
            agent_id,
            behavior,
            exchange.factory(),
            done=threading.Event(),
        )
        self._acbs[agent_id] = acb
        self._launch(agent_id)

        return exchange.get_handle(agent_id)

    def running(self) -> set[AgentId[Any]]:
        """Get a set of IDs for all running agents.

        Returns:
            Set of agent IDs corresponding to all agents launched by this \
            launcher that have not completed yet.
        """
        running: set[AgentId[Any]] = set()
        for acb in self._acbs.values():
            if not acb.done.is_set():
                running.add(acb.agent_id)
        return running

    def wait(
        self,
        agent_id: AgentId[Any],
        *,
        ignore_error: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Wait for a launched agent to exit.

        Note:
            Calling `wait()` is only valid after `launch()` has succeeded.

        Args:
            agent_id: ID of launched agent.
            ignore_error: Ignore any errors raised by the agent.
            timeout: Optional timeout in seconds to wait for agent.

        Raises:
            BadEntityIdError: If an agent with `agent_id` was not
                launched by this launcher.
            TimeoutError: If `timeout` was exceeded while waiting for agent.
            Exception: Any exception raised by the agent if
                `ignore_error=False`.
        """
        try:
            acb = self._acbs[agent_id]
        except KeyError:
            raise BadEntityIdError(agent_id) from None

        if not acb.done.wait(timeout):
            raise TimeoutError(
                f'Agent did not complete within {timeout}s timeout '
                f'({acb.agent_id})',
            )

        # The only time _ACB.future is None is between constructing the _ACB
        # in launch() and creating the future in _launch().
        assert acb.future is not None
        # _ACB.done event should only be set in callback of future so
        # the future must be done.
        assert acb.future.done()

        if not ignore_error:
            exc = acb.future.exception()
            if exc is not None:
                raise exc


class ThreadLauncher(Launcher):
    """Launcher that wraps a default [`concurrent.futures.ThreadPoolExecutor`][concurrent.futures.ThreadPoolExecutor].

    Args:
        max_workers: The maximum number of threads (i.e., agents) in the pool.
        max_restarts: Maximum times to restart an agent if it exits with
            an error.
    """  # noqa: E501

    def __init__(
        self,
        max_workers: int | None = None,
        *,
        max_restarts: int = 0,
    ) -> None:
        executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix='launcher',
        )
        super().__init__(
            executor,
            close_exchange=False,
            max_restarts=max_restarts,
        )
