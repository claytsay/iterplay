"""Speedrunner bot: can the level be finished? Are all collectibles reachable?"""

import math
from dataclasses import dataclass
from typing import Final, Self

from iterplay.bots.base import Bot, Vector
from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameEventType, GameState, PlayerState
from iterplay.models.playtest import BotName

DEAD_ZONE: Final = 0.2
"""World units a target must lie along a movement direction before the bot steers that way."""

MIN_PROGRESS: Final = 0.05
"""World units closer to its target than ever before the player must get to make progress."""

JUMP_HOLD_S: Final = 0.3
"""How long each jump is held: games with variable-height jumps turn a quick tap into a hop."""

MAX_JUMP_HOLD_S: Final = 1.0
"""Longest a jump is held while the player is still rising, for games slower than real time."""

RESPAWN_AFTER_S: Final = 8.0
"""Seconds without progress, since the player last (re)spawned, before pressing respawn."""

GIVE_UP_AFTER_S: Final = 12.0
"""Seconds without progress toward a collectible before giving up on it."""

_RESPAWN_EVENTS: Final = frozenset({GameEventType.DEATH, GameEventType.RESPAWN})


@dataclass(frozen=True, slots=True)
class _Target:
    collectible_id: str | None
    """None for the goal."""

    position: Vector


@dataclass(slots=True)
class _Progress:
    """The closest the player has come to a target, and when that last improved."""

    best: float
    improved_t: float

    @classmethod
    def starting(cls, now: float) -> Self:
        """A fresh record: the next distance seen becomes the baseline."""
        return cls(best=math.inf, improved_t=now)

    def update(self, distance: float, now: float) -> None:
        if distance < self.best - MIN_PROGRESS:
            self.best = distance
            self.improved_t = now


class SpeedrunnerBot(Bot):
    """A greedy controller.

    Steers toward the nearest uncollected collectible, then the goal, using positions from
    ``getState()`` rather than the screen and the control scheme's world directions (left
    and right along x, in 2D). Jumps when blocked (no progress for ``blocked_after_s``) or on
    walking off a ledge, while coyote time still allows it.

    Progress means getting :data:`MIN_PROGRESS` closer to the target than ever before. It
    survives deaths, so after respawning behind its best point the bot hops its way back.
    Each jump is held for :data:`JUMP_HOLD_S`, and while the player still rises for up to
    :data:`MAX_JUMP_HOLD_S`, so variable-height jumps reach full height. Within
    ``arrive_radius`` of a collectible the bot stops steering to let the pickup happen,
    creeping closer only once progress stalls. It gives up on a collectible after
    :data:`GIVE_UP_AFTER_S` without progress, so it still heads for the goal, and presses
    respawn only to recover from :data:`RESPAWN_AFTER_S` without progress since the player
    last spawned. Once the level is complete it holds nothing.
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
        self._target: _Target | None = None
        self._progress = _Progress.starting(0.0)
        self._since_spawn = _Progress.starting(0.0)
        self._given_up: set[str] = set()
        self._jump_t = -math.inf
        self._held_jump = False
        self._was_grounded = False
        self._deaths: int | None = None

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        held = self._plan(state, video_t)
        self._was_grounded = state.player.grounded
        self._held_jump = Control.JUMP in held
        return held

    def _plan(self, state: GameState, now: float) -> frozenset[Control]:
        if self._respawned(state):
            self._since_spawn = _Progress.starting(now)
        if state.level_complete:
            return frozenset()
        here = _position(state.player)
        target, distance = self._follow(state, here, now)
        stuck = self._elapsed(self._since_spawn.improved_t, now, RESPAWN_AFTER_S)
        if stuck and self._is_bound(Control.RESPAWN):
            self._since_spawn = _Progress.starting(now)
            return frozenset({Control.RESPAWN})
        held = self._steer(here, target, distance, now)
        if self._jump(state.player, now):
            held.append(Control.JUMP)
        return self._bound(held)

    def _respawned(self, state: GameState) -> bool:
        """Whether the player died or respawned since the previous tick."""
        died = self._deaths is not None and state.deaths > self._deaths
        self._deaths = state.deaths
        return died or any(event.type in _RESPAWN_EVENTS for event in state.events)

    def _follow(self, state: GameState, here: Vector, now: float) -> tuple[_Target, float]:
        """The target to steer for and its distance, giving up on a collectible if stuck."""
        target, distance = self._track(state, here, now)
        if target.collectible_id is not None and self._elapsed(
            self._progress.improved_t, now, GIVE_UP_AFTER_S
        ):
            self._given_up.add(target.collectible_id)
            target, distance = self._track(state, here, now)
        return target, distance

    def _track(self, state: GameState, here: Vector, now: float) -> tuple[_Target, float]:
        """The nearest target and its distance, after recording progress toward it."""
        target = self._nearest_target(state, here)
        if self._target is None or target.collectible_id != self._target.collectible_id:
            self._progress = _Progress.starting(now)
            self._since_spawn = _Progress.starting(now)
        self._target = target
        distance = math.dist(here, target.position)
        self._progress.update(distance, now)
        self._since_spawn.update(distance, now)
        return target, distance

    def _nearest_target(self, state: GameState, here: Vector) -> _Target:
        """The nearest uncollected collectible not given up on, else the goal."""
        open_items = [
            (item.id, (item.x, item.y, item.z))
            for item in state.collectibles
            if not item.collected and item.id not in self._given_up
        ]
        if not open_items:
            goal = state.goal
            return _Target(None, (goal.x, goal.y, goal.z))
        collectible_id, position = min(open_items, key=lambda item: math.dist(here, item[1]))
        return _Target(collectible_id, position)

    def _steer(self, here: Vector, target: _Target, distance: float, now: float) -> list[Control]:
        """Movement controls pointing at the target by more than the dead zone, strongest first."""
        stalled = self._elapsed(self._progress.improved_t, now, self._blocked_after_s)
        arrived = target.collectible_id is not None and distance <= self._arrive_radius
        if arrived and not stalled:
            return []
        offset = (
            target.position[0] - here[0],
            target.position[1] - here[1],
            target.position[2] - here[2],
        )
        pulls = {control: self._along(control, offset) for control in self._movement}
        toward = (c for c in pulls if pulls[c] > DEAD_ZONE)
        return self._without_opposites(sorted(toward, key=pulls.__getitem__, reverse=True))

    def _jump(self, player: PlayerState, now: float) -> bool:
        """Whether to hold jump, finishing the current jump or starting one when blocked or
        on walking off a ledge.

        After a jump the key is released for at least one tick, so every jump is a fresh
        press.
        """
        if self._held_jump:
            rising = not player.grounded and player.vy > 0
            return not self._elapsed(self._jump_t, now, JUMP_HOLD_S) or (
                rising and not self._elapsed(self._jump_t, now, MAX_JUMP_HOLD_S)
            )
        walked_off = self._was_grounded and not player.grounded and player.vy <= 0
        blocked = player.grounded and self._elapsed(
            max(self._progress.improved_t, self._jump_t), now, self._blocked_after_s
        )
        if walked_off or blocked:
            self._jump_t = now
            return True
        return False


def _position(player: PlayerState) -> Vector:
    return (player.x, player.y, player.z)
