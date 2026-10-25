"""Speedrunner bot: can the level be finished? Are all collectibles reachable?"""

from iterplay.bots.base import Bot
from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName


class SpeedrunnerBot(Bot):
    """A greedy controller.

    Steers toward the nearest uncollected collectible, then the goal, using positions from
    ``getState()`` rather than the screen and the control scheme's world directions (left
    and right along x, in 2D). Jumps when blocked (no progress for ``blocked_after_s``) or on
    walking off a ledge, while coyote time still allows it.
    """

    name = BotName.SPEEDRUNNER

    def __init__(
        self,
        seed: int,
        controls: ControlScheme,
        *,
        blocked_after_s: float = 0.5,
        arrive_radius: float = 0.75,
    ) -> None:
        super().__init__(seed, controls)
        self._blocked_after_s = blocked_after_s
        self._arrive_radius = arrive_radius

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        raise NotImplementedError
