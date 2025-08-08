from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from collections.abc import Generator
from typing import Any
from unittest import mock

import pytest

logger = logging.getLogger(__name__)


class MockRedis:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.events: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.timeouts: dict[str, asyncio.Future[Any]] = {}

    async def aclose(self) -> None:
        pass

    async def blpop(
        self,
        keys: list[str],
        timeout: float = 0,
    ) -> list[str] | None:
        result: list[str] = []
        for key in keys:
            if len(self.lists[key]) > 0:
                item = self.lists[key].pop()
                self.events[key].clear()
            else:
                try:
                    await asyncio.wait_for(
                        self.events[key].wait(),
                        timeout=None if timeout == 0 else timeout,
                    )
                except asyncio.TimeoutError:
                    return None
                else:
                    item = self.lists[key].pop()
                    self.events[key].clear()
            result.extend([key, item])
        return result

    async def delete(self, key: str) -> None:  # pragma: no cover
        if key in self.values:
            del self.values[key]
        elif key in self.lists:
            self.lists[key].clear()

    async def exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.values or key in self.lists

    async def _expire_key(self, key, timeout: int):
        await asyncio.sleep(timeout)
        logger.info(f'Key {key} expired.')
        await self.delete(key)
        self.events[key].clear()
        self.timeouts.pop(key, None)

    async def expire(  # noqa: PLR0913
        self,
        key: str,
        time: int,
        nx: bool = False,
        xx: bool = False,
        gt: bool = False,
        lt: bool = False,
    ) -> None:
        if nx and key in self.timeouts:
            return

        if xx or gt or lt:
            raise NotImplementedError()

        if key in self.timeouts:
            self.timeouts[key].cancel()
            self.timeouts.pop(key, None)

        self.timeouts[key] = asyncio.ensure_future(
            self._expire_key(key, time),
        )

    async def get(
        self,
        key: str,
    ) -> str | list[str] | None:
        if key in self.values:
            return self.values[key]
        elif key in self.lists:
            raise NotImplementedError()
        return None

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, None)
        if items is None:
            return []
        end = end + 1 if end >= 0 else len(items) + end + 1
        return items[start:end]

    async def ping(self, **kwargs) -> None:
        pass

    async def rpush(self, key: str, *values: str) -> None:
        for value in values:
            self.lists[key].append(value)
            self.events[key].set()

    async def scan_iter(self, pattern: str) -> AsyncGenerator[str]:
        for key in self.values:
            if fnmatch.fnmatch(key, pattern):
                yield key

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value


@pytest.fixture
def mock_redis() -> Generator[None]:
    redis = MockRedis()
    with mock.patch('redis.asyncio.Redis', return_value=redis):
        yield
