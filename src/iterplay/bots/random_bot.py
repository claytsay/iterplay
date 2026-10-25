"""Random bot: finds crashes, falls through the floor, stuck spots and out-of-bounds."""

import math
from itertools import combinations
from typing import Final

from iterplay.bots.base import Bot
from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName

JUMP_CHANCE: Final = 0.4
"""Share of holds that include jump."""

RESPAWN_CHANCE: Final = 0.02
"""Share of holds that start with a tap of respawn."""


class RandomBot(Bot):
    """Holds random combinations of the scheme's controls for random durations.

    Each hold lasts between ``min_hold_s`` and ``max_hold_s`` seconds of video time, drawn
    uniformly. It picks, with equal chance, one combination of movement controls without
    two opposites (possibly none), and adds jump to :data:`JUMP_CHANCE` of holds.
    :data:`RESPAWN_CHANCE` of holds start with a single-tick tap of respawn, so a game that
    respawns while the key is down is not stuck respawning.
    """

    name = BotName.RANDOM

    def __init__(
        self,
        seed: int,
        controls: ControlScheme,
        *,
        min_hold_s: float = 0.2,
        max_hold_s: float = 1.5,
    ) -> None:
        super().__init__(seed, controls)
        self._min_hold_s = min_hold_s
        self._max_hold_s = max_hold_s
        self._moves = self._move_combinations()
        self._held: frozenset[Control] = frozenset()
        self._hold_start = -math.inf
        self._hold_s = 0.0

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        if not self._elapsed(self._hold_start, video_t, self._hold_s):
            return self._held
        self._hold_start = video_t
        self._hold_s = self._rng.uniform(self._min_hold_s, self._max_hold_s)
        held = set(self._rng.choice(self._moves))
        if self._rng.random() < JUMP_CHANCE:
            held.add(Control.JUMP)
        self._held = self._bound(held)
        if self._rng.random() < RESPAWN_CHANCE:
            return self._bound({*self._held, Control.RESPAWN})
        return self._held

    def _move_combinations(self) -> tuple[frozenset[Control], ...]:
        """Every combination of movement controls with no two opposites, including none."""
        return tuple(
            frozenset(combination)
            for size in range(len(self._movement) + 1)
            for combination in combinations(self._movement, size)
            if len(self._without_opposites(combination)) == size
        )
