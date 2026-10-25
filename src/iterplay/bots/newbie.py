"""Newbie bot: finds unforgiving jumps, unclear goals and frustrating difficulty."""

from iterplay.bots.base import Bot
from iterplay.bots.speedrunner import SpeedrunnerBot
from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName


class NewbieBot(Bot):
    """A Speedrunner with human flaws: input lag, overshoot and random pauses.

    Wraps a :class:`SpeedrunnerBot` and perturbs its decisions rather than re-planning.
    ``pause_chance`` is the per-tick chance of starting a pause lasting ``pause_s`` seconds
    (uniformly drawn from the range).
    """

    name = BotName.NEWBIE

    def __init__(
        self,
        seed: int,
        controls: ControlScheme,
        *,
        input_lag_s: float = 0.3,
        overshoot_s: float = 0.25,
        pause_chance: float = 0.03,
        pause_s: tuple[float, float] = (0.5, 2.0),
    ) -> None:
        super().__init__(seed, controls)
        self._planner = SpeedrunnerBot(seed, controls)
        self._input_lag_s = input_lag_s
        self._overshoot_s = overshoot_s
        self._pause_chance = pause_chance
        self._pause_s = pause_s

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        raise NotImplementedError
