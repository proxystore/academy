from __future__ import annotations

import functools
import logging
import sys
import time
import uuid
from collections.abc import Iterable
from collections.abc import Mapping
from concurrent.futures import Future
from concurrent.futures import wait
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

from academy.exception import HandleClosedError
from academy.exception import HandleNotBoundError
from academy.exception import MailboxClosedError
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import PingRequest
from academy.message import PingResponse
from academy.message import RequestMessage
from academy.message import ResponseMessage
from academy.message import ShutdownRequest
from academy.message import ShutdownResponse

if TYPE_CHECKING:
    from academy.behavior import Behavior
    from academy.exchange import BoundExchangeClient
else:
    # Behavior is only used in the bounding of the BehaviorT TypeVar.
    Behavior = None

logger = logging.getLogger(__name__)

K = TypeVar('K')
P = ParamSpec('P')
R = TypeVar('R')
BehaviorT = TypeVar('BehaviorT', bound=Behavior)


@runtime_checkable
class Handle(Protocol[BehaviorT]):
    """Agent handle protocol.

    A handle enables a client or agent to invoke actions on another agent.
    """

    agent_id: AgentId[BehaviorT]
    mailbox_id: EntityId | None

    def __getattr__(self, name: str) -> Any:
        # This dummy method definition is required to signal to mypy that
        # any attribute access is "valid" on a Handle type. This forces
        # mypy into calling our mypy plugin (academy.mypy_plugin) which then
        # validates the exact semantics of the attribute access depending
        # on the concrete type for the BehaviorT that Handle is generic on.
        ...

    def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        ...

    def close(
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

    def ping(self, *, timeout: float | None = None) -> float:
        """Ping the agent.

        Ping the agent and wait to get a response. Agents process messages
        in order so the round-trip time will include processing time of
        earlier messages in the queue.

        Args:
            timeout: Optional timeout in seconds to wait for the response.

        Returns:
            Round-trip time in seconds.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            TimeoutError: If the timeout is exceeded.
        """
        ...

    def shutdown(self) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        ...


class HandleDict(dict[K, Handle[BehaviorT]]):
    """Dictionary mapping keys to handles.

    Tip:
        The `HandleDict` is required when storing a mapping of handles as
        attributes of a `Behavior` so that those handles get bound to the
        correct agent when running.
    """

    def __init__(
        self,
        values: Mapping[K, Handle[BehaviorT]]
        | Iterable[tuple[K, Handle[BehaviorT]]] = (),
        /,
        **kwargs: dict[str, Handle[BehaviorT]],
    ) -> None:
        super().__init__(values, **kwargs)


class HandleList(list[Handle[BehaviorT]]):
    """List of handles.

    Tip:
        The `HandleList` is required when storing a list of handles as
        attributes of a `Behavior` so that those handles get bound to the
        correct agent when running.
    """

    def __init__(
        self,
        iterable: Iterable[Handle[BehaviorT]] = (),
        /,
    ) -> None:
        super().__init__(iterable)


class ProxyHandle(Generic[BehaviorT]):
    """Proxy handle.

    A proxy handle is thin wrapper around a
    [`Behavior`][academy.behavior.Behavior] instance that is useful for testing
    behaviors that are initialized with a handle to another agent without
    needing to spawn agents. This wrapper invokes actions synchronously.
    """

    def __init__(self, behavior: BehaviorT) -> None:
        self.behavior = behavior
        self.agent_id: AgentId[BehaviorT] = AgentId.new()
        self.mailbox_id: EntityId | None = None
        self._agent_closed = False
        self._handle_closed = False

    def __repr__(self) -> str:
        return f'{type(self).__name__}(behavior={self.behavior!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<{self.behavior}>'

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.behavior, name)
        if not callable(method):
            raise AttributeError(
                f'Attribute {name} of {type(self.behavior)} is not a method.',
            )

        @functools.wraps(method)
        def func(*args: Any, **kwargs: Any) -> Future[R]:
            return self.action(name, *args, **kwargs)

        return func

    def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        if self._agent_closed:
            raise MailboxClosedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)

        future: Future[R] = Future()
        try:
            method = getattr(self.behavior, action)
            result = method(*args, **kwargs)
        except Exception as e:
            future.set_exception(e)
        else:
            future.set_result(result)
        return future

    def close(
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

    def ping(self, *, timeout: float | None = None) -> float:
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
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            TimeoutError: If the timeout is exceeded.
        """
        if self._agent_closed:
            raise MailboxClosedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)
        return 0

    def shutdown(self) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        if self._agent_closed:
            raise MailboxClosedError(self.agent_id)
        elif self._handle_closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)
        self._agent_closed = True


class UnboundRemoteHandle(Generic[BehaviorT]):
    """Handle to a remote agent that is unbound.

    Warning:
        An unbound handle must be bound before use. Otherwise all methods
        will raise an `HandleNotBoundError` when attempting to send a message
        to the remote agent.

    Args:
        exchange: Message exchange used for agent communication.
        agent_id: EntityId of the agent.
    """

    def __init__(
        self,
        agent_id: AgentId[BehaviorT],
    ) -> None:
        self.agent_id = agent_id
        self.mailbox_id = None

    def __repr__(self) -> str:
        name = type(self).__name__
        return f'{name}(agent_id={self.agent_id!r})'

    def __str__(self) -> str:
        return f'{type(self).__name__}<agent: {self.agent_id}>'

    def __getattr__(self, name: str) -> Any:
        """Raises `AttributeError`."""
        raise AttributeError(self, name=name)

    def bind_to_exchange(
        self,
        exchange: BoundExchangeClient,
    ) -> BoundRemoteHandle[BehaviorT]:
        """Bind the handle to an existing mailbox.

        Args:
            exchange: Client exchange to associate with handle

        Returns:
            Remote handle bound to the identifier.
        """
        return exchange.get_handle(self.agent_id)

    def _send_request(self, request: RequestMessage) -> None:
        raise HandleNotBoundError(self.agent_id)

    def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[R]:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    def close(self) -> None:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    def ping(self, *, timeout: float | None = None) -> float:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)

    def shutdown(self) -> None:
        """Raises [`HandleNotBoundError`][academy.exception.HandleNotBoundError]."""  # noqa: E501
        raise HandleNotBoundError(self.agent_id)


class BoundRemoteHandle(Generic[BehaviorT]):
    """Handle to a remote agent bound to an existing mailbox.

    Args:
        exchange: Message exchange used for agent communication.
        agent_id: EntityId of the target agent of this handle.
        mailbox_id: EntityId of the mailbox this handle receives messages to.
    """

    def __init__(
        self,
        exchange: BoundExchangeClient,
        agent_id: AgentId[BehaviorT],
        mailbox_id: EntityId | None = None,
    ) -> None:
        if agent_id == mailbox_id:
            raise ValueError(
                f'Cannot create handle to {agent_id} that is bound to itself. '
                'Check that the values of `agent_id` and `mailbox_id` '
                'are different.',
            )

        self.exchange = exchange
        self.agent_id = agent_id
        self.mailbox_id = mailbox_id
        # Unique identifier for each handle object; used to disambiguate
        # messages when multiple handles are bound to the same mailbox.
        self.handle_id = uuid.uuid4()

        self._futures: dict[uuid.UUID, Future[Any]] = {}
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __reduce__(
        self,
    ) -> tuple[
        type[UnboundRemoteHandle[Any]],
        tuple[AgentId[BehaviorT]],
    ]:
        return (UnboundRemoteHandle, (self.agent_id,))

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(agent_id={self.agent_id!r}, '
            f'mailbox_id={self.mailbox_id!r}, exchange={self.exchange!r})'
        )

    def __str__(self) -> str:
        name = type(self).__name__
        return f'{name}<agent: {self.agent_id}; mailbox: {self.mailbox_id}>'

    def __getattr__(self, name: str) -> Any:
        def remote_method_call(*args: Any, **kwargs: Any) -> Future[R]:
            return self.action(name, *args, **kwargs)

        return remote_method_call

    def clone(self) -> UnboundRemoteHandle[BehaviorT]:
        """Create an unbound copy of this handle."""
        return UnboundRemoteHandle(self.agent_id)

    def _process_response(self, response: ResponseMessage) -> None:
        if isinstance(response, (ActionResponse, PingResponse)):
            future = self._futures.pop(response.tag)
            if response.exception is not None:
                future.set_exception(response.exception)
            elif isinstance(response, ActionResponse):
                future.set_result(response.result)
            elif isinstance(response, PingResponse):
                future.set_result(None)
            else:
                raise AssertionError('Unreachable.')
        elif isinstance(response, ShutdownResponse):  # pragma: no cover
            # Shutdown responses are not implemented yet.
            pass
        else:
            raise AssertionError('Unreachable.')

    def _send_request(self, request: RequestMessage) -> None:
        self.exchange.send(request.dest, request)

    def close(
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
        self._closed = True

        if len(self._futures) == 0:
            return
        if wait_futures:
            logger.debug('Waiting on pending futures for %s', self)
            wait(list(self._futures.values()), timeout=timeout)
        else:
            logger.debug('Cancelling pending futures for %s', self)
            for future in self._futures:
                self._futures[future].cancel()

    def action(
        self,
        action: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[R]:
        """Invoke an action on the agent.

        Args:
            action: Action to invoke.
            args: Positional arguments for the action.
            kwargs: Keywords arguments for the action.

        Returns:
            Future to the result of the action.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        if self.mailbox_id is None:
            # UnboundRemoteHandle overrides these methods and is the only
            # handle state variant where hid is None.
            raise AssertionError(
                'Method should not be reachable in unbound state.',
            )
        if self._closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)

        request = ActionRequest(
            src=self.mailbox_id,
            dest=self.agent_id,
            label=self.handle_id,
            action=action,
            pargs=args,
            kargs=kwargs,
        )
        future: Future[R] = Future()
        self._futures[request.tag] = future
        self._send_request(request)
        logger.debug(
            'Sent action request from %s to %s (action=%r)',
            self.mailbox_id,
            self.agent_id,
            action,
        )
        return future

    def ping(self, *, timeout: float | None = None) -> float:
        """Ping the agent.

        Ping the agent and wait to get a response. Agents process messages
        in order so the round-trip time will include processing time of
        earlier messages in the queue.

        Args:
            timeout: Optional timeout in seconds to wait for the response.

        Returns:
            Round-trip time in seconds.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
            TimeoutError: If the timeout is exceeded.
        """
        if self.mailbox_id is None:
            # UnboundRemoteHandle overrides these methods and is the only
            # handle state variant where hid is None.
            raise AssertionError(
                'Method should not be reachable in unbound state.',
            )
        if self._closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)

        start = time.perf_counter()
        request = PingRequest(
            src=self.mailbox_id,
            dest=self.agent_id,
            label=self.handle_id,
        )
        future: Future[None] = Future()
        self._futures[request.tag] = future
        self._send_request(request)
        logger.debug('Sent ping from %s to %s', self.mailbox_id, self.agent_id)
        future.result(timeout=timeout)
        elapsed = time.perf_counter() - start
        logger.debug(
            'Received ping from %s to %s in %.1f ms',
            self.mailbox_id,
            self.agent_id,
            elapsed * 1000,
        )
        return elapsed

    def shutdown(self) -> None:
        """Instruct the agent to shutdown.

        This is non-blocking and will only send the message.

        Raises:
            HandleClosedError: If the handle was closed.
            MailboxClosedError: If the agent's mailbox was closed. This
                typically indicates the agent shutdown for another reason
                (it self terminated or via another handle).
        """
        if self.mailbox_id is None:
            # UnboundRemoteHandle overrides these methods and is the only
            # handle state variant where hid is None.
            raise AssertionError(
                'Method should not be reachable in unbound state.',
            )
        if self._closed:
            raise HandleClosedError(self.agent_id, self.mailbox_id)

        request = ShutdownRequest(
            src=self.mailbox_id,
            dest=self.agent_id,
            label=self.handle_id,
        )
        self._send_request(request)
        logger.debug(
            'Sent shutdown request from %s to %s',
            self.mailbox_id,
            self.agent_id,
        )

    def bind_to_exchange(
        self,
        exchange: BoundExchangeClient,
    ) -> BoundRemoteHandle[BehaviorT]:
        """Bind the handle to an existing mailbox.

        Args:
            exchange: Client exchange to associate with handle

        Returns:
            Remote handle bound to the identifier.
        """
        return self.clone().bind_to_exchange(exchange)
