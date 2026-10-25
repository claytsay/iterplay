"""Smoke-test report (HLD §4 [2])."""

from enum import StrEnum

from pydantic import Field

from iterplay.models.base import StrictModel


class SmokeCheck(StrEnum):
    """Checks every build must pass before it is playtested."""

    READY = "ready"
    """``window.iterplay.ready`` becomes true within the ready timeout (10 s)."""

    STATE_SCHEMA = "state_schema"
    """``getState()`` returns data matching :class:`~iterplay.contract.state.GameState`."""

    NO_ERRORS = "no_errors"
    """No uncaught exceptions, console errors, or blocked network requests."""

    PLAYER_MOVES = "player_moves"
    """While the template's ``smoke_move`` control (right, in 2D) is held for 3 s, the player's
    position changes in at least one ``getState()`` sample. Comparing only the first and last
    samples would fail a player who fell, died and respawned at the start."""


class SmokeCheckResult(StrictModel):
    check: SmokeCheck
    passed: bool
    detail: str = ""


class SmokeReport(StrictModel):
    """Contents of ``smoke.json``."""

    passed: bool
    checks: list[SmokeCheckResult]
    uncaught_errors: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    attempt: int = Field(ge=1, description="1 for the first build, +1 per quick Fixer retry.")
