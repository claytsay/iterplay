"""Bot persona interface (HLD §4 [3])."""

import random
from abc import ABC, abstractmethod
from typing import ClassVar

from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName


class Bot(ABC):
    """A policy from the latest snapshot to the set of controls to hold.

    The playtest runner calls :meth:`decide` once per tick with the snapshot it has just
    recorded, then holds the returned controls until the next tick by sending real keyboard
    events. Bots only use controls bound in the game template's scheme and steer with its
    world directions, so the same bot plays 2D and 3D templates. All randomness comes from
    the seeded ``self._rng``, so runs are reproducible.
    """

    name: ClassVar[BotName]

    def __init__(self, seed: int, controls: ControlScheme) -> None:
        self._rng = random.Random(seed)
        self._controls = controls

    @abstractmethod
    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        """Controls to hold until the next tick.

        ``video_t`` is seconds since the recording started, the clock the critic sees.
        """
