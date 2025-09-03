from __future__ import annotations

import asyncio
import logging
import multiprocessing
import uuid
from concurrent.futures import ProcessPoolExecutor

from academy.agent import action
from academy.agent import Agent
from academy.exchange.cloud.globus import GlobusExchangeFactory
from academy.logging import init_logging
from academy.manager import Manager


class Echo(Agent):
    """Agent that echos back a string."""

    def __init__(self) -> None:
        super().__init__()

    @action
    async def echo(self, text: str) -> str:
        """Echos a string.

        Args:
            text: input to repeat
        Returns:
            The same string
        """
        return text


async def test_full_globus_exchange_client() -> None:
    """Test the full exchange client.

    This test can be used to test the hosted exchange with production
    Globus Auth. However, we don't mock enough of the responses to
    run this as part of CI/CD integration testing.
    """
    init_logging(logging.DEBUG)

    factory = GlobusExchangeFactory(
        project_id=uuid.UUID('183dd9a1-b344-44ff-b968-d2a3499f9c65'),
        client_params={'base_url': 'http://0.0.0.0:8700'},
    )
    mp_context = multiprocessing.get_context('spawn')
    executor = ProcessPoolExecutor(
        max_workers=1,
        initializer=init_logging,
        initargs=(logging.INFO,),
        mp_context=mp_context,
    )

    async with await Manager.from_exchange_factory(
        factory=factory,
        executors=executor,
    ) as manager:
        echo = await manager.launch(Echo)
        text = 'DEADBEEF'
        result = await echo.echo(text)
        assert result == text


if __name__ == '__main__':
    raise SystemExit(asyncio.run(test_full_globus_exchange_client()))
