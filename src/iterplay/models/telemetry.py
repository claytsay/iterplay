"""Telemetry records and the digest given to the critic (HLD §4 [3], [5])."""

from pydantic import Field

from iterplay.contract.state import GameState, Vec3
from iterplay.models.base import StrictModel
from iterplay.models.playtest import BotName


class TelemetryRecord(StrictModel):
    """One line of ``<bot>.telemetry.jsonl``: a ``getState()`` snapshot and when it was taken."""

    video_t: float = Field(
        ge=0,
        description="Seconds since this bot's recording started; matches the burned-in mm:ss.",
    )
    state: GameState


class TimedPosition(StrictModel):
    """Where the player was when something happened."""

    video_t: float = Field(ge=0)
    position: Vec3


class OutOfBounds(TimedPosition):
    """An ``oob`` event."""

    intentional: bool = Field(description="Fell into a designed pit, not through geometry.")


class StuckInterval(StrictModel):
    """A stretch in which the player barely moved while the level was incomplete."""

    start_video_t: float = Field(ge=0)
    end_video_t: float = Field(ge=0)
    position: Vec3


class FpsStats(StrictModel):
    min: float = Field(ge=0)
    median: float = Field(ge=0)


class BotTelemetrySummary(StrictModel):
    """Pre-computed digest of one bot's telemetry."""

    bot: BotName
    completed: bool
    time_to_complete_s: float | None = Field(
        description="Game-time seconds until levelComplete; None if never completed."
    )
    deaths: list[TimedPosition]
    out_of_bounds: list[OutOfBounds]
    stuck_intervals: list[StuckInterval]
    fps: FpsStats | None = Field(description="None when no snapshots were recorded.")
    collected_ids: list[str]


class TelemetrySummary(StrictModel):
    """Contents of ``playtest/telemetry_summary.json``; the critic gets this, not raw JSONL."""

    bots: list[BotTelemetrySummary]
    never_collected_ids: list[str] = Field(description="Collectibles no bot collected.")
