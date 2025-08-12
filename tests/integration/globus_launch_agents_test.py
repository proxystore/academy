from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import logging

import pytest

from academy.agent import action
from academy.agent import Agent
from academy.exchange.cloud.globus import GlobusExchangeFactory
from academy.handle import Handle
from academy.manager import Manager
from academy.socket import open_port
from academy.logging import init_logging


class Echo(Agent):
    def __init__(self) -> None:
        super().__init__()

    @action
    async def echo(self, text: str) -> str:
        print("Running action")
        return text

async def test_run_in_processes() -> None:
    init_logging('INFO')

    factory = GlobusExchangeFactory(
        project_id="183dd9a1-b344-44ff-b968-d2a3499f9c65"
    )
    mp_context = multiprocessing.get_context('spawn')
    executor = ProcessPoolExecutor(
        max_workers=3,
        initializer=init_logging,
        initargs=(logging.INFO,),
        mp_context=mp_context,
    )

    async with await Manager.from_exchange_factory(
        factory=factory,
        executors=executor,
    ) as manager:
        echo = await manager.launch(Echo)
        print("Launched all agents.")

        text = 'DEADBEEF'
        result = await echo.echo(text)
        print("Got result")
        assert result == text

        print("Shutting down.")

if __name__ == "__main__":
    SystemExit(asyncio.run(test_run_in_processes()))