from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import sys
import warnings
from collections.abc import Iterable
from collections.abc import MutableMapping
from concurrent.futures import Executor
from types import TracebackType
from typing import Any
from typing import Generic

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.agent import AgentT
from academy.exception import AgentTerminatedError
from academy.exception import BadEntityIdError
from academy.exception import raise_exceptions
from academy.exchange import ExchangeFactory
from academy.exchange import UserExchangeClient
from academy.exchange.transport import AgentRegistration
from academy.exchange.transport import ExchangeTransportT
from academy.handle import RemoteHandle
from academy.identifier import AgentId
from academy.identifier import UserId
from academy.runtime import Runtime
from academy.runtime import RuntimeConfig
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _RunSpec(Generic[AgentT, ExchangeTransportT]):
    agent: AgentT | type[AgentT]
    config: RuntimeConfig
    exchange_factory: ExchangeFactory[ExchangeTransportT]
    registration: AgentRegistration[AgentT]
    agent_args: tuple[Any, ...]
    agent_kwargs: dict[str, Any]


async def _run_agent_on_worker_async(
    spec: _RunSpec[AgentT, ExchangeTransportT],
) -> None:
    try:
        if isinstance(spec.agent, type):
            agent = spec.agent(*spec.agent_args, **spec.agent_kwargs)
        else:
            agent = spec.agent

        async with Runtime(
            agent,
            config=spec.config,
            exchange_factory=spec.exchange_factory,
            registration=spec.registration,
        ) as runtime:
            await runtime.wait_shutdown()
    except:
        logger.exception(
            'Failure running agent on worker (%s)',
            spec.registration.agent_id,
        )
        raise


def _run_agent_on_worker(
    spec: _RunSpec[AgentT, ExchangeTransportT],
) -> None:
    asyncio.run(_run_agent_on_worker_async(spec))


@dataclasses.dataclass
class _ACB(Generic[AgentT]):
    # Agent Control Block
    agent_id: AgentId[AgentT]
    executor: str
    task: asyncio.Task[None]


class Manager(Generic[ExchangeTransportT], NoPickleMixin):
    """Launch and manage running agents.

    A manager is used to launch agents using one or more
    [`Executors`][concurrent.futures.Executor] and interact with/manage those
    agents.

    Tip:
        This class can be used as a context manager. Upon exiting the context,
        running agents will be shutdown, any agent handles created by the
        manager will be closed, and the executors will be shutdown.

    Tip:
        When using
        [`ProcessPoolExecutors`][concurrent.futures.ProcessPoolExecutor],
        use the `initializer` argument to configure logging in the worker
        processes that will execute agents.

    Note:
        The manager takes ownership of the exchange client and executors,
        meaning the manager will clean up those resources when the manager
        is closed.

    Args:
        exchange_client: Exchange client.
        executors: An executor instance or mapping of names to executors to
            use to run agents. If a single executor is provided, it is set
            as the default executor with name `'default'`, overriding any
            value of `default_executor`.
        default_executor: Specify the name of the default executor to use
            when not specified in `launch()`.
        max_retries: Maximum number of times to retry running an agent
            if it exits with an error.

    Raises:
        ValueError: If `default_executor` is specified but does not exist
            in `executors`.
    """

    def __init__(
        self,
        exchange_client: UserExchangeClient[ExchangeTransportT],
        executors: Executor | MutableMapping[str, Executor],
        *,
        default_executor: str | None = None,
        max_retries: int = 0,
    ) -> None:
        if isinstance(executors, Executor):
            executors = {'default': executors}
            default_executor = 'default'

        if default_executor is not None and default_executor not in executors:
            raise ValueError(
                f'No executor named "{default_executor}" was provided to '
                'use as the default.',
            )

        self._exchange_client = exchange_client
        self._exchange_factory = exchange_client.factory()
        self._user_id = self._exchange_client.client_id
        self._executors = executors
        self._default_executor = default_executor
        self._max_retries = max_retries

        self._handles: dict[AgentId[Any], RemoteHandle[Any]] = {}
        self._acbs: dict[AgentId[Any], _ACB[Any]] = {}

        logger.info('Initialized manager (%s)', self.user_id)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __repr__(self) -> str:
        executors_repr = ', '.join(
            f'{k}: {v!r}' for k, v in self._executors.items()
        )
        return (
            f'{type(self).__name__}'
            f'(exchange={self._exchange_client!r}, '
            f'executors={{{executors_repr}}})'
        )

    def __str__(self) -> str:
        return (
            f'{type(self).__name__}<{self.user_id}, {self._exchange_client}>'
        )

    @classmethod
    async def from_exchange_factory(
        cls,
        factory: ExchangeFactory[ExchangeTransportT],
        executors: Executor | MutableMapping[str, Executor],
        *,
        default_executor: str | None = None,
        max_retries: int = 0,
    ) -> Self:
        """Instantiate a new exchange client and manager from a factory."""
        client = await factory.create_user_client()
        return cls(
            client,
            executors,
            default_executor=default_executor,
            max_retries=max_retries,
        )

    @property
    def exchange_client(self) -> UserExchangeClient[ExchangeTransportT]:
        """User client for the exchange."""
        return self._exchange_client

    @property
    def exchange_factory(self) -> ExchangeFactory[ExchangeTransportT]:
        """Client factory for the exchange."""
        return self._exchange_factory

    @property
    def user_id(self) -> UserId:
        """Exchange client user ID of this manager."""
        return self._user_id

    async def close(self) -> None:
        """Shutdown the manager and cleanup resources.

        1. Request all running agents to shut down.
        1. Wait for all running agents to shut down.
        1. Close the exchange client.
        1. Shutdown the executors.
        1. Raise an exceptions returned by agents.

        Raises:
            Exception: Any exceptions raised by agents.
        """
        for acb in self._acbs.values():
            if not acb.task.done():
                handle = self.get_handle(acb.agent_id)
                with contextlib.suppress(AgentTerminatedError):
                    await handle.shutdown()
        logger.debug('Requested shutdown from all agents')

        for acb in self._acbs.values():
            await acb.task
        logger.debug('All agents have completed')

        await self.exchange_client.close()
        for executor in self._executors.values():
            executor.shutdown(wait=True, cancel_futures=True)

        exceptions = (acb.task.exception() for acb in self._acbs.values())
        exceptions_only = tuple(e for e in exceptions if e is not None)
        raise_exceptions(
            exceptions_only,
            message='Caught failures in agent while shutting down.',
        )

        logger.info('Closed manager (%s)', self.user_id)

    def add_executor(self, name: str, executor: Executor) -> Self:
        """Add an executor to the manager.

        Note:
            It is not possible to remove an executor as this could create
            complications if an agent is already running in that executor.

        Args:
            name: Name of the executor used when launching agents.
            executor: Executor instance.

        Returns:
            Self for chaining.

        Raises:
            ValueError: If an executor with `name` already exists.
        """
        if name in self._executors:
            raise ValueError(f'Executor named "{name}" already exists.')
        self._executors[name] = executor
        return self

    def set_default_executor(self, name: str | None) -> Self:
        """Set the default executor by name.

        Args:
            name: Name of the executor to use as default. If `None`, no
                default executor is set and all calls to `launch()` must
                specify the executor.

        Returns:
            Self for chaining.

        Raises:
            ValueError: If no executor with `name` exists.
        """
        if name not in self._executors:
            raise ValueError(f'An executor named "{name}" does not exist.')
        self._default_executor = name
        return self

    async def _run_agent_in_executor(
        self,
        executor: Executor,
        spec: _RunSpec[AgentT, ExchangeTransportT],
    ) -> None:
        agent_id = spec.registration.agent_id
        original_config = spec.config
        loop = asyncio.get_running_loop()
        run_count = 0
        retries = self._max_retries

        while True:
            run_count += 1
            if retries > 0:
                retries -= 1
                # Override this configuration for the case where the agent
                # fails and we will be restarting it.
                spec.config = dataclasses.replace(
                    original_config,
                    terminate_on_error=False,
                )
            else:
                # Otherwise, keep the original config.
                spec.config = original_config

            logger.debug(
                'Launching agent (attempt: %s; retries: %s; %s; %s)',
                run_count,
                retries,
                agent_id,
                spec.agent,
            )

            try:
                await loop.run_in_executor(
                    executor,
                    _run_agent_on_worker,
                    spec,
                )
            except asyncio.CancelledError:  # pragma: no cover
                logger.warning('Cancelled %s task', agent_id)
                raise
            except Exception:
                if retries == 0:
                    logger.exception('Received exception from %s', agent_id)
                    raise
                else:
                    logger.exception(
                        'Restarting %s due to exception',
                        agent_id,
                    )
            else:
                logger.debug('Completed %s task', agent_id)
                break

    async def launch(  # noqa: PLR0913
        self,
        agent: AgentT | type[AgentT],
        *,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        config: RuntimeConfig | None = None,
        executor: str | None = None,
        name: str | None = None,
        registration: AgentRegistration[AgentT] | None = None,
    ) -> RemoteHandle[AgentT]:
        """Launch a new agent with a specified agent.

        Args:
            agent: Agent instance the agent will implement or the
                agent type that will be initialized on the worker using
                `args` and `kwargs`.
            args: Positional arguments used to initialize the agent.
                Ignored if `agent` is already an instance.
            kwargs: Keyword arguments used to initialize the agent.
                Ignored if `agent` is already an instance.
            config: Agent run configuration.
            executor: Name of the executor instance to use. If `None`, uses
                the default executor, if specified, otherwise raises an error.
            name: Readable name of the agent used when registering a new agent.
            registration: If `None`, a new agent will be registered with
                the exchange.

        Returns:
            Handle (client bound) used to interact with the agent.

        Raises:
            RuntimeError: If `registration` is provided and an agent with
                that ID has already been executed.
            ValueError: If no default executor is set and `executor` is not
                specified.
        """
        if self._default_executor is None and executor is None:
            raise ValueError(
                'Must specify the executor when no default is set.',
            )
        executor = executor if executor is not None else self._default_executor
        assert executor is not None
        executor_instance = self._executors[executor]

        if registration is None:
            agent_type = agent if isinstance(agent, type) else type(agent)
            registration = await self.register_agent(agent_type, name=name)
        elif registration.agent_id in self._acbs:
            raise RuntimeError(
                f'{registration.agent_id} has already been executed.',
            )

        agent_id = registration.agent_id

        spec = _RunSpec(
            agent=agent,
            config=RuntimeConfig() if config is None else config,
            exchange_factory=self.exchange_factory,
            registration=registration,
            agent_args=() if args is None else args,
            agent_kwargs={} if kwargs is None else kwargs,
        )

        task = asyncio.create_task(
            self._run_agent_in_executor(executor_instance, spec),
            name=f'manager-run-{agent_id}',
        )

        acb = _ACB(agent_id=agent_id, executor=executor, task=task)
        self._acbs[agent_id] = acb
        handle = self.get_handle(agent_id)
        logger.info('Launched agent (%s; %s)', agent_id, agent)
        self._warn_executor_overloaded(executor_instance, executor)
        return handle

    def get_handle(
        self,
        agent: AgentId[AgentT] | AgentRegistration[AgentT],
    ) -> RemoteHandle[AgentT]:
        """Create a new handle to an agent.

        A handle acts like a reference to a remote agent, enabling a user
        to manage the agent or asynchronously invoke actions.

        Args:
            agent: Agent ID or registration indicating the agent to create
                a handle to. The agent must be registered with the same
                exchange that this manager is a client of.

        Returns:
            Handle to the agent.
        """
        agent_id = agent if isinstance(agent, AgentId) else agent.agent_id
        handle = self._handles.get(agent_id, None)
        if handle is not None and not handle.closed():
            return handle
        handle = self.exchange_client.get_handle(agent_id)
        self._handles[agent_id] = handle
        return handle

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> AgentRegistration[AgentT]:
        """Register a new agent with the exchange.

        Args:
            agent: Agent type of the agent.
            name: Optional display name for the agent.

        Returns:
            Agent registration info that can be passed to
            [`launch()`][academy.manager.Manager.launch].
        """
        return await self.exchange_client.register_agent(agent, name=name)

    def running(self) -> set[AgentId[Any]]:
        """Get a set of IDs of all running agents.

        Returns:
            Set of agent IDs corresponding to all agents launched by this \
            manager that have not completed yet.
        """
        running: set[AgentId[Any]] = set()
        for acb in self._acbs.values():
            if not acb.task.done():
                running.add(acb.agent_id)
        return running

    async def shutdown(
        self,
        agent: AgentId[Any] | RemoteHandle[Any],
        *,
        blocking: bool = True,
        raise_error: bool = True,
        terminate: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        """Shutdown a launched agent.

        Args:
            agent: ID or handle to the launched agent.
            blocking: Wait for the agent to exit before returning.
            raise_error: Raise the error returned by the agent if
                `blocking=True`.
            terminate: Override the termination behavior of the agent defined
                in the [`RuntimeConfig`][academy.runtime.RuntimeConfig].
            timeout: Optional timeout is seconds when `blocking=True`.

        Raises:
            BadEntityIdError: If an agent with `agent_id` was not
                launched by this manager.
            TimeoutError: If `timeout` was exceeded while blocking for agent.
        """
        agent_id = agent.agent_id if isinstance(agent, RemoteHandle) else agent

        if agent_id not in self._acbs:
            raise BadEntityIdError(agent_id) from None
        if self._acbs[agent_id].task.done():
            return

        handle = self.get_handle(agent_id)
        with contextlib.suppress(AgentTerminatedError):
            await handle.shutdown(terminate=terminate)

        if blocking:
            await self.wait(
                {agent_id},
                raise_error=raise_error,
                timeout=timeout,
            )

    async def wait(
        self,
        agents: Iterable[AgentId[Any] | RemoteHandle[Any]],
        *,
        raise_error: bool = False,
        return_when: str = asyncio.ALL_COMPLETED,
        timeout: float | None = None,
    ) -> None:
        """Wait for launched agents to complete.

        Note:
            Calling `wait()` is only valid after `launch()` has succeeded.

        Args:
            agents: An iterable of agent IDs or handles to wait on.
            raise_error: Raise errors returned by completed agents.
            return_when: Indicate when this function should return. The
                same as [`asyncio.wait()`][asyncio.wait].
            timeout: Optional timeout in seconds to wait for agents.

        Raises:
            BadEntityIdError: If an agent was not launched by this manager.
            TimeoutError: If `timeout` was exceeded while waiting for agents.
            Exception: Any exception raised by an agent that completed due
                to a failure and `raise_error=True` is set.
        """
        agent_ids = {
            agent if isinstance(agent, AgentId) else agent.agent_id
            for agent in agents
        }

        if len(agent_ids) == 0:
            return

        agent_tasks: list[asyncio.Task[None]] = []
        for agent_id in agent_ids:
            try:
                agent_tasks.append(self._acbs[agent_id].task)
            except KeyError:
                raise BadEntityIdError(agent_id) from None

        done, pending = await asyncio.wait(
            agent_tasks,
            return_when=return_when,
            timeout=timeout,
        )

        if len(done) == 0:
            raise TimeoutError(
                f'No agents completed within {timeout} seconds: '
                f'{len(pending)} pending agent(s).',
            )
        elif return_when == asyncio.ALL_COMPLETED and len(pending) > 0:
            raise TimeoutError(
                f'Not all agents completed within {timeout} seconds: '
                f'{len(pending)} pending agent(s).',
            )

        if raise_error:
            exceptions = (task.exception() for task in agent_tasks)
            exceptions_only = tuple(e for e in exceptions if e is not None)
            raise_exceptions(
                exceptions_only,
                message='Waited agents raised the following exceptions.',
            )

    def _warn_executor_overloaded(
        self,
        executor: Executor,
        name: str,
    ) -> None:
        max_workers = _infer_max_workers(executor)
        if max_workers is None:  # pragma: no cover
            # If the user provided a third-party executor that we don't
            # know how to get the number of workers for just return.
            return
        running_agents = len(self.running())
        if running_agents > max_workers:
            warnings.warn(
                f'Executor overload: submitted agents exceeds worker count '
                f'(executor: {name}, running agents: {running_agents}, max '
                f'workers: {max_workers})',
                RuntimeWarning,
                # This stacklevel ensures the user see the line that called
                # Manager.launch(), rather than seeing this line or the
                # call to _warn_executor_overloaded
                stacklevel=3,
            )


def _infer_max_workers(executor: Executor) -> int | None:  # pragma: no cover
    """Infer the maximum workers of the executor.

    The [`Executor`][concurrent.futures.Executor] specification does not
    provide a standard mechanism to get the maximum number of workers.
    """
    if hasattr(executor, '_max_workers'):
        # ProcessPoolExecutor and ThreadPoolExecutor
        return executor._max_workers
    elif hasattr(executor, 'scheduler_info') and callable(
        executor.scheduler_info,
    ):
        # Dask distributed client
        try:
            return executor.scheduler_info()['workers']
        except KeyError:
            return None
    else:
        return None
