from __future__ import annotations

import logging
import multiprocessing
import uuid
from concurrent.futures import ProcessPoolExecutor

import pytest

from academy.agent import action
from academy.agent import Agent
from academy.exchange.cloud.globus import GlobusExchangeFactory
from academy.logging import init_logging
from academy.manager import Manager


class Echo(Agent):
    def __init__(self) -> None:  # pragma: no cover
        super().__init__()

    @action
    async def echo(self, text: str) -> str:  # pragma: no cover
        print('Running action')
        return text


@pytest.mark.skip(reason='Responses not mocked sufficiently.')
async def test_full_globus_exchange_client() -> None:  # pragma: no cover
    """Test the full exchange client.

    This test can be used to test the hosted exchange with production
    Globus Auth. However, we don't mock enough of the responses to
    run this as part of CI/CD integration testing.
    TODO: Replay responses from Globus based on logged file.
    """
    factory = GlobusExchangeFactory(
        project_id=uuid.UUID('183dd9a1-b344-44ff-b968-d2a3499f9c65'),
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
