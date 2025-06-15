from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import MutableMapping
from types import TracebackType
from typing import Any
from typing import TypeVar

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.behavior import Behavior
from academy.exception import BadEntityIdError
from academy.exception import MailboxClosedError
from academy.exchange import ExchangeClient
from academy.exchange import ExchangeFactory
from academy.handle import BoundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import ClientId
from academy.launcher import Launcher
from academy.serialize import NoPickleMixin

logger = logging.getLogger(__name__)

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


class Manager(NoPickleMixin):
    """Launch and manage running agents.

    The manager is provided as convenience to reduce common boilerplate code
    for spawning agents and managing handles. Each manager registers itself
    as a client in the exchange (i.e., each manager has its own mailbox).
    Handles created by the manager are bound to this mailbox.

    Tip:
        This class can be used as a context manager. Upon exiting the context,
        running agents will be shutdown, any agent handles created by the
        manager will be closed, and the exchange and launcher will be closed.

    Note:
        The manager takes ownership of the exchange and launcher interfaces.
        This means the manager will be responsible for closing them once the
        manager is closed.

    Args:
        exchange: Exchange that agents and clients will use for communication.
        launcher: A mapping of names to launchers used to execute agents
            remotely. If a single launcher is provided directly, it is
            set as the default with name `'default'`, overriding any value
            of `default_launcher`.
        default_launcher: Specify the name of the default launcher to use
            when not specified in `launch()`.

    Raises:
        ValueError: If `default_launcher` is specified but does not exist
            in `launchers`.
    """

    def __init__(
        self,
        exchange: ExchangeFactory,
        launcher: Launcher | MutableMapping[str, Launcher],
        *,
        default_launcher: str | None = None,
    ) -> None:
        if isinstance(launcher, Launcher):
            launcher = {'default': launcher}
            default_launcher = 'default'

        if default_launcher is not None and default_launcher not in launcher:
            raise ValueError(
                f'No launcher named "{default_launcher}" was provided to '
                'use as the default.',
            )

        self._exchange = exchange.create_user_client()
        self._mailbox_id = self._exchange.user_id
        self._launchers = launcher
        self._default_launcher = default_launcher

        self._handles: dict[AgentId[Any], BoundRemoteHandle[Any]] = {}
        self._aid_to_launcher: dict[AgentId[Any], str] = {}

        logger.info(
            'Initialized manager (%s; %s)',
            self._mailbox_id,
            self._exchange,
        )

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
        launchers_repr = ', '.join(
            f'{k}: {v!r}' for k, v in self._launchers.items()
        )
        return (
            f'{type(self).__name__}'
            f'(exchange={self._exchange!r}, launchers={{{launchers_repr}}})'
        )

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.mailbox_id}, {self._exchange}>'

    @property
    def exchange(self) -> ExchangeClient:
        """Exchange interface."""
        return self._exchange

    @property
    def mailbox_id(self) -> ClientId:
        """EntityId of the mailbox used by this manager."""
        return self._mailbox_id

    def close(self) -> None:
        """Close the manager and cleanup resources.

        1. Call shutdown on all running agents.
        1. Close all handles created by the manager.
        1. Close the mailbox associated with the manager.
        1. Close the exchange.
        1. Close all launchers.
        """
        for launcher in self._launchers.values():
            for agent_id in launcher.running():
                handle = self._handles[agent_id]
                with contextlib.suppress(MailboxClosedError):
                    handle.shutdown()
        logger.debug('Instructed managed agents to shutdown')
        self.exchange.close()
        for launcher in self._launchers.values():
            launcher.close()
        logger.info('Closed manager (%s)', self.mailbox_id)

    def add_launcher(self, name: str, launcher: Launcher) -> Self:
        """Add a launcher to the manager.

        Note:
            It is not possible to remove a launcher as this could create
            complications if an agent was already launched using a given
            launcher.

        Args:
            name: Name of the launcher.
            launcher: Launcher instance.

        Returns:
            Self for chaining.

        Raises:
            ValueError: If a launcher with `name` already exists.
        """
        if name in self._launchers:
            raise ValueError(f'Launcher named "{name}" already exists.')
        self._launchers[name] = launcher
        return self

    def set_default_launcher(self, name: str | None) -> Self:
        """Set the name of the default launcher.

        Args:
            name: Name of the launcher to default to. If `None`, no default
                launcher is set and all calls to `launch()` must specify
                the launcher.

        Returns:
            Self for chaining.

        Raises:
            ValueError: If no launcher with `name` exists.
        """
        if name not in self._launchers:
            raise ValueError(f'A launcher name "{name}" does not exist.')
        self._default_launcher = name
        return self

    def launch(
        self,
        behavior: BehaviorT,
        *,
        agent_id: AgentId[BehaviorT] | None = None,
        launcher: str | None = None,
        name: str | None = None,
    ) -> BoundRemoteHandle[BehaviorT]:
        """Launch a new agent with a specified behavior.

        Note:
            Compared to `Launcher.launch()`, this method will inject the
            exchange and return a client-bound handle.

        Args:
            behavior: Behavior the agent should implement.
            agent_id: Specify ID of the launched agent. If `None`, a new
                agent ID will be created within the exchange.
            launcher: Name of the launcher instance to use. In `None`, uses
                the default launcher if specified, otherwise raises an error.
            name: Readable name of the agent. Ignored if `agent_id` is
                provided.

        Returns:
            Handle (client bound) used to interact with the agent.

        Raises:
            ValueError: If no default launcher is set and `launcher` is not
                specified.
        """
        if self._default_launcher is None and launcher is None:
            raise ValueError(
                'Must specify the launcher when no default is set.',
            )
        launcher = launcher if launcher is not None else self._default_launcher
        assert launcher is not None
        launcher_instance = self._launchers[launcher]
        bound = launcher_instance.launch(
            behavior,
            exchange=self.exchange,
            agent_id=agent_id,
            name=name,
        )
        self._aid_to_launcher[bound.agent_id] = launcher
        logger.info('Launched agent (%s; %s)', bound.agent_id, behavior)
        self._handles[bound.agent_id] = bound
        logger.debug('Bound agent handle to manager (%s)', bound)
        return bound

    def shutdown(
        self,
        agent_id: AgentId[Any],
        *,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Shutdown a launched agent.

        Args:
            agent_id: ID of launched agent.
            blocking: Wait for the agent to exit before returning.
            timeout: Optional timeout is seconds when `blocking=True`.

        Raises:
            BadEntityIdError: If an agent with `agent_id` was not
                launched by this launcher.
            TimeoutError: If `timeout` was exceeded while blocking for agent.
        """
        try:
            handle = self._handles[agent_id]
        except KeyError:
            raise BadEntityIdError(agent_id) from None

        with contextlib.suppress(MailboxClosedError):
            handle.shutdown()

        if blocking:
            self.wait(agent_id, timeout=timeout)

    def wait(
        self,
        agent: AgentId[Any] | BoundRemoteHandle[Any],
        *,
        timeout: float | None = None,
    ) -> None:
        """Wait for a launched agent to exit.

        Args:
            agent: ID or handle to the launched agent.
            timeout: Optional timeout in seconds to wait for agent.

        Raises:
            BadEntityIdError: If the agent was not found. This likely means
                the agent was not launched by this launcher.
            TimeoutError: If `timeout` was exceeded while waiting for agent.
        """
        agent_id = (
            agent.agent_id if isinstance(agent, BoundRemoteHandle) else agent
        )
        if agent_id not in self._aid_to_launcher:
            raise BadEntityIdError(agent_id)
        launcher_name = self._aid_to_launcher[agent_id]
        self._launchers[launcher_name].wait(agent_id, timeout=timeout)
