from __future__ import annotations

import logging
from concurrent.futures import Future

from academy.behavior import action
from academy.behavior import Behavior
from academy.exchange.thread import UnboundThreadExchangeClient
from academy.handle import Handle
from academy.launcher import ThreadLauncher
from academy.logging import init_logging
from academy.manager import Manager

logger = logging.getLogger(__name__)


class Coordinator(Behavior):
    def __init__(
        self,
        lowerer: Handle[Lowerer],
        reverser: Handle[Reverser],
    ) -> None:
        self.lowerer = lowerer
        self.reverser = reverser

    @action
    def process(self, text: str) -> str:
        text = self.lowerer.action('lower', text).result()
        text = self.reverser.action('reverse', text).result()
        return text


class Lowerer(Behavior):
    @action
    def lower(self, text: str) -> str:
        return text.lower()


class Reverser(Behavior):
    @action
    def reverse(self, text: str) -> str:
        return text[::-1]


def main() -> int:
    init_logging(logging.INFO)

    with Manager(
        exchange=UnboundThreadExchangeClient(),
        launcher=ThreadLauncher(),
    ) as manager:
        lowerer = manager.launch(Lowerer())
        reverser = manager.launch(Reverser())
        coordinator = manager.launch(Coordinator(lowerer, reverser))

        text = 'DEADBEEF'
        expected = 'feebdaed'

        future: Future[str] = coordinator.action('process', text)
        logger.info('Invoking process("%s") on %s', text, coordinator.agent_id)
        assert future.result() == expected
        logger.info('Received result: "%s"', future.result())

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
