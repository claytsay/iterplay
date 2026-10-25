"""Playtest outcomes (HLD §4 [3])."""

from enum import StrEnum

from pydantic import Field

from iterplay.models.base import StrictModel


class BotName(StrEnum):
    """Bot personas, in the order their clips appear in the combined video (HLD §4 [4])."""

    RANDOM = "random"
    SPEEDRUNNER = "speedrunner"
    NEWBIE = "newbie"


class RunEnd(StrEnum):
    """Why a bot's session ended."""

    LEVEL_COMPLETE = "level_complete"
    TIME_LIMIT = "time_limit"
    CRASHED = "crashed"


class BotRunResult(StrictModel):
    """Outcome of one bot session. Its video and telemetry paths come from ``VersionPaths``."""

    bot: BotName
    bot_seed: int
    ended_by: RunEnd
    duration_s: float = Field(ge=0, description="Recorded seconds, at most the time limit.")
    uncaught_errors: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    crash_detail: str | None = None


class PlaytestReport(StrictModel):
    """Contents of ``playtest/report.json``: every bot session for one version."""

    game_seed: int
    runs: list[BotRunResult]
