"""[2] Smoke test, so a playtest is never wasted on a broken build (HLD §4 [2])."""

import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from iterplay.contract.controls import ControlScheme
from iterplay.contract.state import HARNESS_GLOBAL, GameState, PlayerState, Vec3
from iterplay.contract.url import GameUrlParams
from iterplay.errors import ContractError
from iterplay.harness.browser import BLOCKED_REQUEST, GameSession, HarnessBrowser, game_url
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport

SAMPLE_INTERVAL_S: Final = 0.1
"""How often ``getState()`` is sampled while the movement control is held."""

MIN_MOVEMENT: Final = 1e-3
"""World units a coordinate must change by to count as movement rather than float noise."""

_READY_FLAG: Final = f"window.{HARNESS_GLOBAL}.ready"


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    """Raises ValueError unless both durations are positive."""

    seed: int = 1
    ready_timeout_s: float = 10.0
    move_hold_s: float = 3.0

    def __post_init__(self) -> None:
        if not self.ready_timeout_s > 0:
            raise ValueError(f"ready_timeout_s must be positive, got {self.ready_timeout_s}")
        if not self.move_hold_s > 0:
            raise ValueError(f"move_hold_s must be positive, got {self.move_hold_s}")


class SmokeTester:
    """Loads ``index.html?seed=1&fixedStep=1`` in a headless browser and runs every
    :class:`~iterplay.models.smoke.SmokeCheck`.

    The movement check holds ``controls.smoke_move`` for ``move_hold_s`` seconds.

    The checks run in order, each with a detail saying what was seen: ready (within
    ``ready_timeout_s`` of starting to load the page), state schema, player moves, and last
    no errors, so that it also covers anything thrown while the player moved. A check whose
    prerequisite failed fails too, with a ``skipped: <reason>`` detail. The report lists the
    checks in :class:`~iterplay.models.smoke.SmokeCheck` order.
    """

    def __init__(
        self,
        browser: HarnessBrowser,
        controls: ControlScheme,
        config: SmokeConfig | None = None,
    ) -> None:
        """Prepare a smoke test driven through ``controls``.

        Raises:
            ValueError: ``controls`` binds no key to its ``smoke_move`` control.
        """
        if not controls.bindings.get(controls.smoke_move):
            raise ValueError(f"The control scheme has no key binding for {controls.smoke_move}")
        self._browser = browser
        self._controls = controls
        self._config = config or SmokeConfig()

    async def run(self, base_url: str, *, attempt: int = 1) -> SmokeReport:
        """Smoke-test the game served at ``base_url``. Failures are reported, never raised."""
        params = GameUrlParams(seed=self._config.seed, fixed_step=True, harness_hud=False)
        async with self._browser.session(self._controls) as session:
            results = await self._check(session, game_url(base_url, params))
            uncaught, console = session.uncaught_errors, session.console_errors
        checks = [results[check] for check in SmokeCheck]
        return SmokeReport(
            passed=all(result.passed for result in checks),
            checks=checks,
            uncaught_errors=list(uncaught),
            console_errors=list(console),
            attempt=attempt,
        )

    async def _check(self, session: GameSession, url: str) -> dict[SmokeCheck, SmokeCheckResult]:
        ready = await self._ready(session, url)
        if ready.passed:
            schema, state = await self._state_schema(session)
        else:
            schema, state = _skipped(SmokeCheck.STATE_SCHEMA, "the game never became ready"), None
        if state is not None:
            moves = await self._player_moves(session)
        elif ready.passed:
            moves = _skipped(SmokeCheck.PLAYER_MOVES, "getState() did not return a valid snapshot")
        else:
            moves = _skipped(SmokeCheck.PLAYER_MOVES, "the game never became ready")
        return {
            SmokeCheck.READY: ready,
            SmokeCheck.STATE_SCHEMA: schema,
            SmokeCheck.PLAYER_MOVES: moves,
            SmokeCheck.NO_ERRORS: _no_errors(session),
        }

    async def _ready(self, session: GameSession, url: str) -> SmokeCheckResult:
        timeout_s = self._config.ready_timeout_s
        started = session.video_t()
        try:
            await session.open(url, timeout_s=timeout_s)
        except ContractError as exc:
            return _failed(SmokeCheck.READY, str(exc))
        remaining = timeout_s - (session.video_t() - started)
        if await session.wait_until_ready(max(remaining, 0.0)):
            elapsed = session.video_t() - started
            return _passed(SmokeCheck.READY, f"{_READY_FLAG} became true after {elapsed:.1f} s.")
        try:
            await session.get_state()
        except ContractError as exc:
            reason = str(exc)
        else:
            reason = f"window.{HARNESS_GLOBAL} exists, but its ready flag is not true"
        detail = f"{_READY_FLAG} did not become true within {timeout_s:g} s: {reason}"
        return _failed(SmokeCheck.READY, detail)

    async def _state_schema(
        self, session: GameSession
    ) -> tuple[SmokeCheckResult, GameState | None]:
        try:
            state = await session.get_state()
        except ContractError as exc:
            return _failed(SmokeCheck.STATE_SCHEMA, str(exc)), None
        collectibles = _count(len(state.collectibles), "collectible")
        detail = (
            f"getState() returned a valid snapshot: the player is at {_point(state.player)}, "
            f"the goal at {_point(state.goal)}, with {collectibles}."
        )
        return _passed(SmokeCheck.STATE_SCHEMA, detail), state

    async def _player_moves(self, session: GameSession) -> SmokeCheckResult:
        control = self._controls.smoke_move
        held = f"{control} ({self._controls.bindings[control][0]})"
        hold_s = self._config.move_hold_s
        samples = max(1, math.ceil(hold_s / SAMPLE_INTERVAL_S - 1e-9))
        moved_to: PlayerState | None = None
        try:
            first = (await session.get_state()).player
            await session.hold(frozenset({control}))
            started = session.video_t()
            for sample in range(1, samples + 1):
                await asyncio.sleep(
                    max(0.0, started + sample * SAMPLE_INTERVAL_S - session.video_t())
                )
                player = (await session.get_state()).player
                if moved_to is None and _moved(first, player):
                    moved_to = player
        except ContractError as exc:
            return _failed(SmokeCheck.PLAYER_MOVES, f"While {held} was held: {exc}")
        finally:
            with suppress(ContractError):
                await session.release_all()
        if moved_to is None:
            detail = (
                f"The player stayed at {_point(first)} while {held} was held for {hold_s:g} s "
                f"({samples} getState() samples)."
            )
            return _failed(SmokeCheck.PLAYER_MOVES, detail)
        detail = (
            f"The player moved from {_point(first)} to {_point(moved_to)} while {held} was held."
        )
        return _passed(SmokeCheck.PLAYER_MOVES, detail)


def _no_errors(session: GameSession) -> SmokeCheckResult:
    uncaught, console = session.uncaught_errors, session.console_errors
    if not uncaught and not console:
        return _passed(
            SmokeCheck.NO_ERRORS, "No uncaught errors, console errors or blocked requests."
        )
    counts = [
        _count(len(errors), noun)
        for errors, noun in ((uncaught, "uncaught error"), (console, "console error"))
        if errors
    ]
    blocked = sum(error.startswith(BLOCKED_REQUEST) for error in console)
    including = f", including {_count(blocked, 'blocked request')}" if blocked else ""
    return _failed(SmokeCheck.NO_ERRORS, f"{' and '.join(counts)}{including}.")


def _moved(first: PlayerState, player: PlayerState) -> bool:
    """Whether ``player`` is anywhere other than ``first``; a NaN coordinate never counts."""
    return any(
        abs(after - before) > MIN_MOVEMENT
        for before, after in ((first.x, player.x), (first.y, player.y), (first.z, player.z))
    )


def _point(position: PlayerState | Vec3) -> str:
    """``(x, y)``, or ``(x, y, z)`` once z is used."""
    coordinates = [position.x, position.y] + ([] if position.z == 0 else [position.z])
    return f"({', '.join(f'{value:.2f}' for value in coordinates)})"


def _passed(check: SmokeCheck, detail: str) -> SmokeCheckResult:
    return SmokeCheckResult(check=check, passed=True, detail=detail)


def _failed(check: SmokeCheck, detail: str) -> SmokeCheckResult:
    return SmokeCheckResult(check=check, passed=False, detail=detail)


def _skipped(check: SmokeCheck, reason: str) -> SmokeCheckResult:
    return _failed(check, f"skipped: {reason}.")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"
