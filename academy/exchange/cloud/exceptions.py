"""Exception types raised by exchange clients and servers."""

from __future__ import annotations


class ExchangeServerError(Exception):
    """Base exception type for exceptions raised by relay clients."""

    pass


class ForbiddenError(ExchangeServerError):
    """Client does not have correct permissions after authentication."""

    pass


class UnauthorizedError(ExchangeServerError):
    """Client is missing authentication tokens."""
