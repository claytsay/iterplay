"""Compact telemetry digest for the critic and Scorer (HLD §4 [5])."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from iterplay.models.playtest import BotName
from iterplay.models.telemetry import BotTelemetrySummary, TelemetryRecord, TelemetrySummary


@dataclass(frozen=True, slots=True)
class SummaryConfig:
    stuck_min_s: float = 1.0
    """Shortest stretch without progress that counts as a stuck interval."""

    stuck_radius: float = 0.25
    """Movement in world units below which the player counts as not progressing."""


def summarize_bot(
    bot: BotName, records: Sequence[TelemetryRecord], config: SummaryConfig
) -> BotTelemetrySummary:
    """Digest one bot's run: completion, deaths, oob events, stuck intervals, FPS, pickups."""
    raise NotImplementedError


def summarize(
    telemetry: Mapping[BotName, Sequence[TelemetryRecord]],
    config: SummaryConfig | None = None,
) -> TelemetrySummary:
    """Digest every bot's run, plus the collectibles that no bot collected."""
    raise NotImplementedError
