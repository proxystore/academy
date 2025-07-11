from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from board import Board
from board import Game
from player import BattleshipPlayer

from academy.agent import action
from academy.agent import Agent
from academy.agent import loop
from academy.handle import Handle

logger = logging.getLogger(__name__)


class Coordinator(Agent):
    _default_ships: ClassVar[list[int]] = [5, 5, 4, 3, 2]

    def __init__(
        self,
        player_0: Handle[BattleshipPlayer],
        player_1: Handle[BattleshipPlayer],
        *,
        size: int = 10,
        ships: list[int] | None = None,
    ) -> None:
        super().__init__()
        self.player_0 = player_0
        self.player_1 = player_1
        self.game_state = Game(Board(), Board())
        self.ships = ships or self._default_ships
        self.stats = [0, 0]

    async def game(self, shutdown: asyncio.Event) -> int:
        while not shutdown.is_set():
            attack = await (await self.player_0.get_move())
            self.game_state.attack(0, attack)
            if self.game_state.check_winner() >= 0:
                return self.game_state.check_winner()

            attack = await (await self.player_1.get_move())
            self.game_state.attack(1, attack)
            if self.game_state.check_winner() >= 0:
                return self.game_state.check_winner()

        return -1

    @loop
    async def play_games(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            player_0_board = await (await self.player_0.new_game(self.ships))
            player_1_board = await (await self.player_1.new_game(self.ships))
            self.game_state = Game(player_0_board, player_1_board)
            winner = await self.game(shutdown)
            self.stats[winner] += 1

    @action
    async def get_game_state(self) -> Game | None:
        return self.game_state

    @action
    async def get_player_stats(self) -> list[int]:
        return self.stats
