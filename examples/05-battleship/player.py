from __future__ import annotations

import asyncio
import random

from board import Board
from board import Crd

from academy.agent import action
from academy.agent import Agent


class BattleshipPlayer(Agent):
    def __init__(
        self,
    ) -> None:
        super().__init__()
        self.guesses = Board()

    @action
    async def get_move(self) -> Crd:
        await asyncio.sleep(1)
        while True:
            row = random.randint(0, self.guesses.size - 1)
            col = random.randint(0, self.guesses.size - 1)
            if self.guesses.receive_attack(Crd(row, col)) != 'already_guessed':
                return Crd(row, col)

    @action
    async def notify_move(self, loc: Crd) -> None:
        # Naive player does not keep track of where guesses
        # happen
        return

    @action
    async def new_game(self, ships: list[int], size: int = 10) -> Board:
        self.guesses = Board(size)
        my_board = Board(size)
        for i, ship in enumerate(ships):
            my_board.place_ship(Crd(i, 0), ship, 'horizontal')
        return my_board
