from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.identifier import UserId


class ActionCancelledError(Exception):
    """Action was cancelled by the agent.

    This often happens when an agent is shutdown mid-action execution and
    configured to cancel running actions.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f'Action "{name}" was cancelled by the agent.')


class AgentNotInitializedError(Exception):
    """Agent runtime context has not been initialized.

    This error is typically raised when accessing the runtime context for
    an agent before the agent has been executed.
    """

    def __init__(self) -> None:
        super().__init__(
            'Agent runtime context has not been initialized. '
            'Has the agent been started?',
        )


class ExchangeError(Exception):
    """Base type for exchange related errors."""

    pass


class BadEntityIdError(ExchangeError):
    """Entity associated with the identifier is unknown."""

    def __init__(self, uid: EntityId) -> None:
        super().__init__(f'Unknown identifier {uid}.')


class ForbiddenError(ExchangeError):
    """Exchange client does not have permission to access resources."""

    pass


class MessageTooLargeError(ExchangeError):
    """Message payload is too large for exchange.

    If encountering this error, consider using the
    [`ProxyStoreExchangeTransport`][academy.exchange.ProxyStoreExchangeTransport]
    class an way to by-pass the exchange for large data.
    """

    pass


class MailboxTerminatedError(ExchangeError):
    """Entity mailbox is terminated and cannot send or receive messages.

    Constructing this error type implicitly returns one of the derived types,
    [`AgentTerminatedError`][academy.exception.AgentTerminatedError] or
    [`UserTerminatedError`][academy.exception.UserTerminatedError], based
    on the entity type.
    """

    def __new__(cls, uid: EntityId) -> MailboxTerminatedError:  # noqa: D102
        if isinstance(uid, AgentId):
            return super().__new__(AgentTerminatedError)
        elif isinstance(uid, UserId):
            return super().__new__(UserTerminatedError)
        else:
            raise AssertionError('Unreachable.')

    def __init__(self, uid: EntityId) -> None:
        super().__init__(f'Mailbox for {uid} has been terminated.')
        self.uid = uid

    def __reduce__(self) -> Any:
        # BaseException implements __reduce__ as
        #     return type(self), self.args
        # where args will contain the message passed to super().__init__
        # rather than uid so it must be customized.
        return type(self), (self.uid,)


class AgentTerminatedError(MailboxTerminatedError):
    """Agent mailbox is terminated and cannot send or receive messages."""

    def __init__(self, uid: AgentId[Any]) -> None:
        super().__init__(uid)


class UserTerminatedError(MailboxTerminatedError):
    """User mailbox is terminated and cannot send or receive messages."""

    def __init__(self, uid: UserId) -> None:
        super().__init__(uid)


class UnauthorizedError(ExchangeError):
    """Exchange client has not provided valid authentication credentials."""

    pass


class ExchangeClientNotFoundError(Exception):
    """Handle to agent can not find an exchange client to use.

    A [`Handle`][academy.handle.Handle] is
    initialized with a target agent ID is not used in a context where an
    exchange client could be inferred. Typically this can be resolved by
    using a [`ExchangeClient`][academy.exchange.ExchangeClient] or
    [`Manager`][academy.manager.Manager] as a context manager. If this error
    happens within an agent, it likely means the agent was not started.
    """

    def __init__(self, aid: AgentId[Any]) -> None:
        super().__init__(
            f'Handle to {aid} can not find an exchange client to use. See the '
            'exception docstring for troubleshooting.',
        )


def raise_exceptions(
    exceptions: Iterable[BaseException],
    *,
    message: str | None = None,
) -> None:
    """Raise exceptions as a group.

    Raises a set of exceptions as an [`ExceptionGroup`][ExceptionGroup]
    in Python 3.11 and later. If only one exception is provided, it is raised
    directly. In Python 3.10 and older, only one exception is raised.

    This is a no-op if the size of `exceptions` is zero.

    Args:
        exceptions: An iterable of exceptions to raise.
        message: Custom error message for the exception group.
    """
    excs = tuple(exceptions)
    if len(excs) == 0:
        return

    if sys.version_info >= (3, 11) and len(excs) > 1:  # pragma: >=3.11 cover
        message = (
            message if message is not None else 'Caught multiple exceptions!'
        )
        # Note that BaseExceptionGroup will return ExceptionGroup if all
        # of the errors are Exception, rather than BaseException, so that this
        # can be caught by "except Exception".
        raise BaseExceptionGroup(message, excs)  # noqa: F821
    else:  # pragma: <3.11 cover
        raise excs[0]
