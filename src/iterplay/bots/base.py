"""Bot persona interface (HLD §4 [3])."""

import random
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import ClassVar, Final

from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName

Vector = tuple[float, float, float]
"""A world-space ``(x, y, z)`` vector, as in :attr:`ControlScheme.directions`."""

_TIME_SLACK_S: Final = 1e-6
"""Tolerance for float error when comparing tick times."""


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
        self._movement = tuple(c for c in controls.directions if self._is_bound(c))

    @abstractmethod
    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        """Controls to hold until the next tick.

        ``video_t`` is seconds since the recording started, the clock the critic sees.
        """

    def _is_bound(self, control: Control) -> bool:
        """Whether the scheme maps ``control`` to at least one key."""
        return bool(self._controls.bindings.get(control))

    def _bound(self, controls: Iterable[Control]) -> frozenset[Control]:
        """The controls the scheme binds, the only ones a bot may return."""
        return frozenset(control for control in controls if self._is_bound(control))

    def _along(self, control: Control, vector: Vector) -> float:
        """How far ``vector`` points along a movement control's world direction."""
        dx, dy, dz = self._controls.directions[control]
        x, y, z = vector
        return dx * x + dy * y + dz * z

    def _opposed(self, first: Control, second: Control) -> bool:
        """Whether two movement controls pull against each other, like left and right."""
        directions = self._controls.directions
        return (
            first in directions
            and second in directions
            and self._along(first, directions[second]) < 0
        )

    def _without_opposites(self, by_priority: Iterable[Control]) -> list[Control]:
        """``by_priority`` without any control opposed to an earlier one."""
        kept: list[Control] = []
        for control in by_priority:
            if not any(self._opposed(control, other) for other in kept):
                kept.append(control)
        return kept

    @staticmethod
    def _elapsed(since: float, now: float, duration_s: float) -> bool:
        """Whether ``duration_s`` has passed between two tick times."""
        return now - since >= duration_s - _TIME_SLACK_S
