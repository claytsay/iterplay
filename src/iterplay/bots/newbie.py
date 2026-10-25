"""Newbie bot: finds unforgiving jumps, unclear goals and frustrating difficulty."""

import math
from collections import deque

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

    The planner sees every snapshot, but the Newbie acts on the decision it made
    ``input_lag_s`` of video time earlier, and holds nothing until the first one is that
    old. Once the planner stops holding a direction, the Newbie keeps holding it for
    ``overshoot_s``, which also delays reversing into the opposite direction. During a
    pause, and once the level is complete, it holds nothing at all.
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
        self._plans: deque[tuple[float, frozenset[Control]]] = deque()
        self._last_plan: frozenset[Control] = frozenset()
        self._released_t: dict[Control, float] = {}
        self._pause_start = -math.inf
        self._pause_length_s = 0.0

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        self._plans.append((video_t, self._planner.decide(state, video_t)))
        held = self._overshoot(self._lagged_plan(video_t), video_t)
        if state.level_complete or self._paused(video_t):
            return frozenset()
        return self._bound(held)

    def _lagged_plan(self, now: float) -> frozenset[Control]:
        """The latest planner decision at least ``input_lag_s`` old, or nothing yet."""
        plans = self._plans
        while len(plans) > 1 and self._elapsed(plans[1][0], now, self._input_lag_s):
            plans.popleft()
        decided_t, plan = plans[0]
        return plan if self._elapsed(decided_t, now, self._input_lag_s) else frozenset()

    def _overshoot(self, plan: frozenset[Control], now: float) -> list[Control]:
        """``plan`` plus directions it let go of under ``overshoot_s`` ago.

        A lingering direction wins over its opposite, so reversing waits for it to expire.
        """
        for control in self._movement:
            if control in plan:
                self._released_t.pop(control, None)
            elif control in self._last_plan:
                self._released_t[control] = now
        self._last_plan = plan
        lingering = [
            control
            for control, released_t in self._released_t.items()
            if not self._elapsed(released_t, now, self._overshoot_s)
        ]
        for control in self._released_t.keys() - lingering:
            del self._released_t[control]
        moves = self._without_opposites([*lingering, *(c for c in self._movement if c in plan)])
        return [*moves, *(c for c in plan if c not in self._movement)]

    def _paused(self, now: float) -> bool:
        """Whether a pause is under way, after possibly starting one at this tick."""
        if not self._elapsed(self._pause_start, now, self._pause_length_s):
            return True
        if self._rng.random() >= self._pause_chance:
            return False
        self._pause_start = now
        self._pause_length_s = self._rng.uniform(*self._pause_s)
        return True
