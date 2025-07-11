from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
import uuid
from collections.abc import Iterable
from collections.abc import Mapping
from types import TracebackType
from typing import Any
from typing import Generic
from typing import Protocol
from typing import runtime_checkable
from typing import TYPE_CHECKING
from typing import TypeVar

if sys.version_info >= (3, 10):  # pragma: >=3.10 cover
    from typing import ParamSpec
else:  # pragma: <3.10 cover
    from typing_extensions import ParamSpec

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from academy.exception import AgentTerminatedError
from academy.exception import HandleClosedError
from academy.exception import HandleNotBoundError
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import PingRequest
from academy.message import Response
from academy.message import ShutdownRequest
from academy.message import SuccessResponse

if TYPE_CHECKING:
    from academy.agent import AgentT
    from academy.exchange import ExchangeClient
else:
    # Agent is only used in the bounding of the AgentT TypeVar.
    AgentT = TypeVar('AgentT')

logger = logging.getLogger(__name__)

K = TypeVar('K')
P = ParamSpec('P')
R = TypeVar('R')


@runtime_checkable
class Handle(Protocol[AgentT]):
    """Agent handle protocol.

    A handle enables an agent or user to invoke actions on another agent.
    """

    def __getattr__(self, name: str) -> Any:
        # This dummy method definition is required to signal to mypy that
        # any attribute access is "valid" on a Handle type. This forces
        # mypy into calling our mypy plugin (academy.mypy_plugin) which then
        # validates the exact semantics of the attribute access depending
        # on the concrete type for the AgentT that Handle is generic on.
        ...

    @property
    def agent_id(self) -> AgentId[AgentT]:
        """ID of the agent this is a handle to."""
        ...

    @property
    def client_id(self) -> EntityId:
        """ID of the client for this handle."""
        ...

    async def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        ...

    async def close(
        self,
        wait_futures: bool = True,
        *,
        timeout: float | None = None,
    ) -> None:
        """Close this handle.

        Args:
            wait_futures: Wait to return until all pending futures are done
                executing. If `False`, pending futures are cancelled.
            timeout: Optional timeout used when `wait=True`.
        """
        ...

    async def ping(self, *, timeout: float | None = None) -> float:
        """Ping the agent.

        Ping the agent and wait to get a response. Agents process messages
        in order so the round-trip time will include processing time of
        earlier messages in the queue.

        Args:
            timeout: Optional timeout in seconds to wait for the response.

        Returns:
            Round-trip time in seconds.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
            TimeoutError: If the timeout is exceeded.
        """
        ...

    async def shutdown(self, *, terminate: bool | None = None) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Args:
            terminate: Override the termination behavior of the agent defined
                in the [`RuntimeConfig`][academy.runtime.RuntimeConfig].

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        ...


class HandleDict(dict[K, Handle[AgentT]]):
    """Dictionary mapping keys to handles.

    Tip:
        The `HandleDict` is required when storing a mapping of handles as
        attributes of a `Agent` so that those handles get bound to the
        correct agent when running.
    """

    def __init__(
        self,
        values: Mapping[K, Handle[AgentT]]
        | Iterable[tuple[K, Handle[AgentT]]] = (),
        /,
        **kwargs: dict[str, Handle[AgentT]],
    ) -> None:
        super().__init__(values, **kwargs)


class HandleList(list[Handle[AgentT]]):
    """List of handles.

    Tip:
        The `HandleList` is required when storing a list of handles as
        attributes of a `Agent` so that those handles get bound to the
        correct agent when running.
    """

    def __init__(
        self,
        iterable: Iterable[Handle[AgentT]] = (),
        /,
    ) -> None:
        super().__init__(iterable)


class ProxyHandle(Generic[AgentT]):
    """Proxy handle.

    A proxy handle is thin wrapper around a
    [`Agent`][academy.agent.Agent] instance that is useful for testing
    agents that are initialized with a handle to another agent without
    needing to spawn agents. This wrapper invokes actions synchronously.
    """

    def __init__(self, agent: AgentT) -> None:
        self.agent = agent
        self.agent_id: AgentId[AgentT] = AgentId.new()
        self.client_id: EntityId = UserId.new()
        self._agent_closed = False
        self._handle_closed = False

    def __repr__(self) -> str:
        return f'{type(self).__name__}(agent={self.agent!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.agent}>'

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.agent, name)
        if not callable(method):
            raise AttributeError(
                f'Attribute {name} of {type(self.agent)} is not a method.',
            )

        @functools.wraps(method)
        async def func(*args: Any, **kwargs: Any) -> asyncio.Future[R]:
            return await self.action(name, *args, **kwargs)

        return func

    async def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        if self._agent_closed:
            raise AgentTerminatedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.client_id)

        future: asyncio.Future[R] = asyncio.get_running_loop().create_future()
        try:
            method = getattr(self.agent, action)
            result = await method(*args, **kwargs)
        except Exception as e:
            future.set_exception(e)
        else:
            future.set_result(result)
        return future

    async def close(
        self,
        wait_futures: bool = True,
        *,
        timeout: float | None = None,
    ) -> None:
        """Close this handle.

        Note:
            This is a no-op for proxy handles.

        Args:
            wait_futures: Wait to return until all pending futures are done
                executing. If `False`, pending futures are cancelled.
            timeout: Optional timeout used when `wait=True`.
        """
        self._handle_closed = True

    async def ping(self, *, timeout: float | None = None) -> float:
        """Ping the agent.

        Ping the agent and wait to get a response. Agents process messages
        in order so the round-trip time will include processing time of
        earlier messages in the queue.

        Note:
            This is a no-op for proxy handles and returns 0 latency.

        Args:
            timeout: Optional timeout in seconds to wait for the response.

        Returns:
            Round-trip time in seconds.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
            TimeoutError: If the timeout is exceeded.
        """
        if self._agent_closed:
            raise AgentTerminatedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.client_id)
        return 0

    async def shutdown(self, *, terminate: bool | None = None) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Args:
            terminate: Override the termination behavior of the agent defined
                in the [`RuntimeConfig`][academy.runtime.RuntimeConfig].

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        if self._agent_closed:
            raise AgentTerminatedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.client_id)
        self._agent_closed = True if terminate is None else terminate


class UnboundRemoteHandle(Generic[AgentT]):
    """Handle to a remote agent that not bound to a mailbox.

    Warning:
        An unbound handle must be bound before use. Otherwise all methods
        will raise an `HandleNotBoundError` when attempting to send a message
        to the remote agent.

    Args:
        agent_id: EntityId of the agent.
    """

    def __init__(self, agent_id: AgentId[AgentT]) -> None:
        self.agent_id = agent_id

    def __repr__(self) -> str:
        name = type(self).__name__
        return f'{name}(agent_id={self.agent_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<agent: {self.agent_id}>'

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            'Actions cannot be invoked via an unbound handle.',
        )

    @property
    def client_id(self) -> EntityId:
        """Raises [`RuntimeError`][RuntimeError] when unbound."""
        raise RuntimeError('An unbound handle has no client ID.')

    def bind_to_client(
        self,
        client: ExchangeClient[Any],
    ) -> RemoteHandle[AgentT]:
        """Bind the handle to an existing mailbox.

        Args:
            client: Exchange client.

        Returns:
            Remote handle bound to the exchange client.
        """
        return client.get_handle(self.agent_id)

    async def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Future[R]:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    async def close(
        self,
        wait_futures: bool = True,
        *,
        timeout: float | None = None,
    ) -> None:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    async def ping(self, *, timeout: float | None = None) -> float:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    async def shutdown(self, *, terminate: bool | None = None) -> None:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)


class RemoteHandle(Generic[AgentT]):
    """Handle to a remote agent bound to an exchange client.

    Args:
        exchange: Exchange client used for agent communication.
        agent_id: EntityId of the target agent of this handle.
    """

    def __init__(
        self,
        exchange: ExchangeClient[Any],
        agent_id: AgentId[AgentT],
    ) -> None:
        self.exchange = exchange
        self.agent_id = agent_id
        self.client_id = exchange.client_id

        if self.agent_id == self.client_id:
            raise ValueError(
                'Cannot create handle to self. The IDs of the exchange '
                f'client and the target agent are the same: {self.agent_id}.',
            )
        # Unique identifier for each handle object; used to disambiguate
        # messages when multiple handles are bound to the same mailbox.
        self.handle_id = uuid.uuid4()

        self._futures: dict[uuid.UUID, asyncio.Future[Any]] = {}
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __reduce__(
        self,
    ) -> tuple[
        type[UnboundRemoteHandle[Any]],
        tuple[AgentId[AgentT]],
    ]:
        return (UnboundRemoteHandle, (self.agent_id,))

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(agent_id={self.agent_id!r}, '
            f'client_id={self.client_id!r}, exchange={self.exchange!r})'
        )

    def __str__(self) -> str:
        name = type(self).__name__
        return f'{name}<agent: {self.agent_id}; mailbox: {self.client_id}>'

    def __getattr__(self, name: str) -> Any:
        async def remote_method_call(
            *args: Any,
            **kwargs: Any,
        ) -> asyncio.Future[R]:
            return await self.action(name, *args, **kwargs)

        return remote_method_call

    def clone(self) -> UnboundRemoteHandle[AgentT]:
        """Create an unbound copy of this handle."""
        return UnboundRemoteHandle(self.agent_id)

    async def _process_response(self, response: Message[Response]) -> None:
        future = self._futures.pop(response.tag, None)
        if future is None or future.cancelled():
            # Response is not associated with any active pending future
            # so safe to ignore it
            return

        body = response.get_body()
        if isinstance(body, ActionResponse):
            future.set_result(body.get_result())
        elif isinstance(body, ErrorResponse):
            future.set_exception(body.exception)
        elif isinstance(body, SuccessResponse):
            future.set_result(None)
        else:
            raise AssertionError('Unreachable.')

    async def close(
        self,
        wait_futures: bool = True,
        *,
        timeout: float | None = None,
    ) -> None:
        """Close this handle.

        Note:
            This does not close the exchange client.

        Args:
            wait_futures: Wait to return until all pending futures are done
                executing. If `False`, pending futures are cancelled.
            timeout: Optional timeout used when `wait=True`.
        """
        self._closed = True

        if len(self._futures) == 0:
            return
        if wait_futures:
            logger.debug('Waiting on pending futures for %s', self)
            await asyncio.wait(
                list(self._futures.values()),
                timeout=timeout,
            )
        else:
            logger.debug('Cancelling pending futures for %s', self)
            for future in self._futures:
                self._futures[future].cancel()

    def closed(self) -> bool:
        """Check if the handle has been closed."""
        return self._closed

    async def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        if self._closed:
            raise HandleClosedError(self.agent_id, self.client_id)

        request = Message.create(
            src=self.client_id,
            dest=self.agent_id,
            label=self.handle_id,
            body=ActionRequest(action=action, pargs=args, kargs=kwargs),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[R] = loop.create_future()
        self._futures[request.tag] = future
        await self.exchange.send(request)
        logger.debug(
            'Sent action request from %s to %s (action=%r)',
            self.client_id,
            self.agent_id,
            action,
        )
        return future

    async def ping(self, *, timeout: float | None = None) -> float:
        """Ping the agent.

        Ping the agent and wait to get a response. Agents process messages
        in order so the round-trip time will include processing time of
        earlier messages in the queue.

        Args:
            timeout: Optional timeout in seconds to wait for the response.

        Returns:
            Round-trip time in seconds.

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
            TimeoutError: If the timeout is exceeded.
        """
        if self._closed:
            raise HandleClosedError(self.agent_id, self.client_id)

        request = Message.create(
            src=self.client_id,
            dest=self.agent_id,
            label=self.handle_id,
            body=PingRequest(),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._futures[request.tag] = future
        start = time.perf_counter()
        await self.exchange.send(request)
        logger.debug('Sent ping from %s to %s', self.client_id, self.agent_id)

        done, pending = await asyncio.wait({future}, timeout=timeout)
        if future in pending:
            raise TimeoutError(
                f'Did not receive ping response within {timeout} seconds.',
            )
        elapsed = time.perf_counter() - start
        logger.debug(
            'Received ping from %s to %s in %.1f ms',
            self.client_id,
            self.agent_id,
            elapsed * 1000,
        )
        return elapsed

    async def shutdown(self, *, terminate: bool | None = None) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Args:
            terminate: Override the termination behavior of the agent defined
                in the [`RuntimeConfig`][academy.runtime.RuntimeConfig].

        Raises:
            AgentTerminatedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            HandleClosedError: If the handle was closed.
        """
        if self._closed:
            raise HandleClosedError(self.agent_id, self.client_id)

        request = Message.create(
            src=self.client_id,
            dest=self.agent_id,
            label=self.handle_id,
            body=ShutdownRequest(terminate=terminate),
        )
        await self.exchange.send(request)
        logger.debug(
            'Sent shutdown request from %s to %s',
            self.client_id,
            self.agent_id,
        )
