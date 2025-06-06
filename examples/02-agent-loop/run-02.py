from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future

from academy.behavior import action
from academy.behavior import Behavior
from academy.behavior import loop
from academy.exchange.thread import UnboundThreadExchangeClient
from academy.launcher import ThreadLauncher
from academy.logging import init_logging
from academy.manager import Manager

logger = logging.getLogger(__name__)


class Counter(Behavior):
    count: int

    def on_setup(self) -> None:
        self.count = 0

    @loop
    def increment(self, shutdown: threading.Event) -> None:
        while not shutdown.is_set():
            time.sleep(1)
            self.count += 1

    @action
    def get_count(self) -> int:
        return self.count


def main() -> int:
    init_logging(logging.INFO)

    with Manager(
        exchange=UnboundThreadExchangeClient(),
        launcher=ThreadLauncher(),
    ) as manager:
        behavior = Counter()
        agent = manager.launch(behavior)

        logger.info('Waiting 2s for agent loops to execute...')
        time.sleep(2)

        future: Future[int] = agent.action('get_count')
        assert future.result() >= 1
        logger.info('Agent loop executed %s time(s)', future.result())

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
