from __future__ import annotations

import asyncio
import dataclasses
import queue
from typing import cast
from typing import Generic
from typing import TypeVar

T = TypeVar('T')

DEFAULT_PRIORITY = 0
CLOSE_PRIORITY = DEFAULT_PRIORITY + 1
CLOSE_SENTINEL = object()


class QueueClosedError(Exception):
    """Queue has been closed exception."""

    pass


@dataclasses.dataclass(order=True)
class _Item(Generic[T]):
    priority: int
    value: T | object = dataclasses.field(compare=False)


class AsyncQueue(Generic[T]):
    """Simple async queue.

    This is a simple backport of Python 3.13 queues which have a shutdown
    method and exception type.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[_Item[T]] = asyncio.PriorityQueue()
        self._closed = False

    async def close(self, immediate: bool = False) -> None:
        """Close the queue.

        This will cause `get` and `put` to raise `QueueClosedError`.

        Args:
            immediate: Close the queue immediately, rather than once the
                queue is empty.
        """
        if not self.closed():
            self._closed = True
            priority = CLOSE_PRIORITY if immediate else DEFAULT_PRIORITY
            await self._queue.put(_Item(priority, CLOSE_SENTINEL))

    def closed(self) -> bool:
        """Check if the queue has been closed."""
        return self._closed

    async def get(self, timeout: float | None = None) -> T:
        """Remove and return the next item from the queue (blocking).

        Args:
            timeout: Block at most `timeout` seconds.

        Raises:
            TimeoutError: if no item was available within `timeout` seconds.
        """
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            # asyncio.TimeoutError is different from TimeoutError in Python
            # 3.10 and older so we convert the error type.
            raise TimeoutError() from None
        if item.value is CLOSE_SENTINEL:
            raise QueueClosedError
        return cast(T, item.value)

    async def put(self, item: T) -> None:
        """Put an item on the queue."""
        if self.closed():
            raise QueueClosedError
        await self._queue.put(_Item(DEFAULT_PRIORITY, item))


class Queue(Generic[T]):
    """Simple queue.

    This is a simple backport of Python 3.13 queues which have a shutdown
    method and exception type.
    """

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue[_Item[T]] = queue.PriorityQueue()
        self._closed = False

    def close(self, immediate: bool = False) -> None:
        """Close the queue.

        This will cause `get` and `put` to raise `QueueClosedError`.

        Args:
            immediate: Close the queue immediately, rather than once the
                queue is empty.
        """
        if not self.closed():
            self._closed = True
            priority = CLOSE_PRIORITY if immediate else DEFAULT_PRIORITY
            self._queue.put(_Item(priority, CLOSE_SENTINEL))

    def closed(self) -> bool:
        """Check if the queue has been closed."""
        return self._closed

    def get(self, timeout: float | None = None) -> T:
        """Remove and return the next item from the queue (blocking).

        Args:
            timeout: Block at most `timeout` seconds.

        Raises:
            TimeoutError: if no item was available within `timeout` seconds.
            QueueClosedError: if the queue was closed.
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if item.value is CLOSE_SENTINEL:
            # Push the sentinel back to the queue in case another thread
            # has called get.
            self._queue.put(_Item(CLOSE_PRIORITY, CLOSE_SENTINEL))
            raise QueueClosedError
        return cast(T, item.value)

    def put(self, item: T) -> None:
        """Put an item on the queue.

        Args:
            item: The item to put on the queue.

        Raises:
            QueueClosedError: if the queue was closed.
        """
        if self.closed():
            raise QueueClosedError
        self._queue.put(_Item(DEFAULT_PRIORITY, item))
