from __future__ import annotations

from typing import Any

from academy.identifier import AgentId
from academy.identifier import EntityId


class BadEntityIdError(Exception):
    """Entity associated with the identifier is unknown."""

    def __init__(self, uid: EntityId) -> None:
        super().__init__(f'Unknown identifier {uid}.')


class HandleClosedError(Exception):
    """Agent handle has been closed."""

    def __init__(
        self,
        agent_id: AgentId[Any],
        mailbox_id: EntityId | None,
    ) -> None:
        message = (
            f'Handle to {agent_id} bound to {mailbox_id} has been closed.'
            if mailbox_id is not None
            else f'Handle to {agent_id} has been closed.'
        )
        super().__init__(message)


class HandleNotBoundError(Exception):
    """Handle to agent is in an unbound state.

    An unbound handle (typically, an instance of `UnboundRemoteHandle`) is
    initialized with a target agent ID and exchange, but does not have an
    identifier itself. Thus, the handle does not have a mailbox in the exchange
    to receive response messages.

    A handle must be bound to be used, either as a unique client with its own
    mailbox or as bound to a running agent where it shares a mailbox with that
    running agent. To create a client bound handle, use
    `handle.bind_to_exchange()`.

    Any agent behavior that has a handle to another agent as an instance
    attribute will be automatically bound to the agent when the agent begins
    running.
    """

    def __init__(self, aid: AgentId[Any]) -> None:
        super().__init__(
            f'Handle to {aid} to an exchange. See the exception docstring '
            'for troubleshooting.',
        )


class MailboxClosedError(Exception):
    """Mailbox is closed and cannot send or receive messages."""

    def __init__(self, uid: EntityId) -> None:
        super().__init__(f'Mailbox for {uid} has been closed.')
