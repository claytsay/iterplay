"""Random bot: finds crashes, falls through the floor, stuck spots and out-of-bounds."""

from iterplay.bots.base import Bot
from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName


class RandomBot(Bot):
    """Holds random combinations of the scheme's controls for random durations."""

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

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        raise NotImplementedError
