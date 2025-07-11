from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from coordinator import Coordinator
from player import BattleshipPlayer

from academy.exchange.cloud.client import spawn_http_exchange
from academy.logging import init_logging
from academy.manager import Manager

EXCHANGE_PORT = 5346
logger = logging.getLogger(__name__)


async def main() -> int:
    init_logging(logging.INFO)

    with spawn_http_exchange('localhost', EXCHANGE_PORT) as factory:
        mp_context = multiprocessing.get_context('spawn')
        executor = ProcessPoolExecutor(
            max_workers=3,
            initializer=init_logging,
            mp_context=mp_context,
        )

        async with await Manager.from_exchange_factory(
            factory=factory,
            # Agents are run by the manager in the processes of this
            # process pool executor.
            executors=executor,
        ) as manager:
            # Launch each of the three agents, each implementing a different
            # behavior. The returned type is a handle to that agent used to
            # invoke actions.
            player_1 = await manager.launch(BattleshipPlayer)
            player_2 = await manager.launch(BattleshipPlayer)
            coordinator = await manager.launch(
                Coordinator,
                args=(player_1, player_2),
            )

            loop = asyncio.get_event_loop()
            while True:
                user_input = await loop.run_in_executor(
                    None,
                    input,
                    'Enter command (exit, game, stat): ',
                )
                if user_input.lower() == 'exit':
                    print('Exiting...')
                    break
                elif user_input.lower() == 'game':
                    game = await (await coordinator.get_game_state())
                    print('Current Game State: ')
                    print(game)
                elif user_input.lower() == 'stat':
                    stats = await (await coordinator.get_player_stats())
                    print(f'Player 0 has won {stats[0]} games')
                    print(f'Player 1 has won {stats[1]} games')
                else:
                    print('Unknown command')
                print('-----------------------------------------------------')

        # Upon exit, the Manager context will instruct each agent to shutdown,
        # closing their respective handles, and shutting down the executors.

    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
