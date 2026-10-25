"""Compact telemetry digest for the critic and Scorer (HLD §4 [5])."""

import math
import statistics
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from iterplay.contract.state import GameEvent, GameEventType, GameState, PlayerState, Vec3
from iterplay.models.playtest import BotName
from iterplay.models.telemetry import (
    BotTelemetrySummary,
    FpsStats,
    OutOfBounds,
    StuckInterval,
    TelemetryRecord,
    TelemetrySummary,
    TimedPosition,
)

_CLIP_ORDER: Final = tuple(BotName)

_RELOCATING_EVENTS: Final = frozenset({GameEventType.DEATH, GameEventType.RESPAWN})
"""Events after which the player is somewhere else by the game's doing, ending a stuck stretch."""


@dataclass(frozen=True, slots=True)
class SummaryConfig:
    stuck_min_s: float = 1.0
    """Shortest stretch without progress that counts as a stuck interval."""

    stuck_radius: float = 0.25
    """Movement in world units below which the player counts as not progressing."""


def summarize_bot(
    bot: BotName, records: Sequence[TelemetryRecord], config: SummaryConfig
) -> BotTelemetrySummary:
    """Digest one bot's run: completion, deaths, oob events, stuck intervals, FPS, pickups.

    ``getState()`` drains events, so each event appears in exactly one record and is dated by
    that record's ``video_t``. An event's position is its own ``x``/``y``/``z`` where it reports
    them, otherwise the player's position in the same snapshot.
    """
    return BotTelemetrySummary(
        bot=bot,
        completed=any(record.state.level_complete for record in records),
        time_to_complete_s=_time_to_complete(records),
        deaths=[
            TimedPosition(video_t=record.video_t, position=_event_position(event, record.state))
            for record, event in _events(records, GameEventType.DEATH)
        ],
        out_of_bounds=[
            OutOfBounds(
                video_t=record.video_t,
                position=_event_position(event, record.state),
                intentional=event.intentional,
            )
            for record, event in _events(records, GameEventType.OOB)
        ],
        stuck_intervals=_stuck_intervals(records, config),
        fps=_fps_stats(records),
        collected_ids=sorted(_collected_ids(records)),
    )


def summarize(
    telemetry: Mapping[BotName, Sequence[TelemetryRecord]],
    config: SummaryConfig | None = None,
) -> TelemetrySummary:
    """Digest every bot's run, plus the collectibles that no bot collected.

    Bots are listed in the order their clips appear in the combined video. A collectible
    exists once any bot's snapshot lists it.
    """
    config = config or SummaryConfig()
    bots = [
        summarize_bot(bot, telemetry[bot], config)
        for bot in sorted(telemetry, key=_CLIP_ORDER.index)
    ]
    seen = {
        collectible.id
        for records in telemetry.values()
        for record in records
        for collectible in record.state.collectibles
    }
    collected = {collectible_id for bot in bots for collectible_id in bot.collected_ids}
    return TelemetrySummary(bots=bots, never_collected_ids=sorted(seen - collected))


def _events(
    records: Sequence[TelemetryRecord], kind: GameEventType
) -> Iterator[tuple[TelemetryRecord, GameEvent]]:
    """Every event of one kind, with the record that carried it."""
    for record in records:
        for event in record.state.events:
            if event.type is kind:
                yield record, event


def _time_to_complete(records: Sequence[TelemetryRecord]) -> float | None:
    """Game time of the first ``levelComplete`` snapshot, or of an earlier ``complete`` event."""
    flagged = next((record.state.t for record in records if record.state.level_complete), None)
    if flagged is None:
        return None
    return min([flagged, *(event.t for _, event in _events(records, GameEventType.COMPLETE))])


def _event_position(event: GameEvent, state: GameState) -> Vec3:
    extra = event.model_extra or {}
    player = state.player
    return Vec3(
        x=_coordinate(extra.get("x"), player.x),
        y=_coordinate(extra.get("y"), player.y),
        z=_coordinate(extra.get("z"), player.z),
    )


def _coordinate(value: object, fallback: float) -> float:
    """``value`` as a finite number, else ``fallback``. Games may report numbers as strings."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return fallback
    try:
        number = float(value)
    except ValueError:
        return fallback
    return number if math.isfinite(number) else fallback


def _stuck_intervals(
    records: Sequence[TelemetryRecord], config: SummaryConfig
) -> list[StuckInterval]:
    return [
        StuckInterval(
            start_video_t=first.video_t,
            end_video_t=last.video_t,
            position=_position(first.state.player),
        )
        for first, last in _still_stretches(records, config.stuck_radius)
        if last.video_t - first.video_t >= config.stuck_min_s
    ]


def _still_stretches(
    records: Sequence[TelemetryRecord], radius: float
) -> Iterator[tuple[TelemetryRecord, TelemetryRecord]]:
    """First and last record of each maximal stretch spent within ``radius`` of its start.

    Stretches cover only snapshots of an incomplete level, and a death or respawn starts a new
    one, so being sent back to a checkpoint never counts as standing still.
    """
    stretch: tuple[TelemetryRecord, TelemetryRecord] | None = None
    for record in records:
        if stretch is not None and _stays_near(stretch[0], record, radius):
            stretch = (stretch[0], record)
            continue
        if stretch is not None:
            yield stretch
        stretch = None if record.state.level_complete else (record, record)
    if stretch is not None:
        yield stretch


def _stays_near(first: TelemetryRecord, record: TelemetryRecord, radius: float) -> bool:
    state = record.state
    return (
        not state.level_complete
        and all(event.type not in _RELOCATING_EVENTS for event in state.events)
        and _distance(first.state.player, state.player) <= radius
    )


def _distance(a: PlayerState, b: PlayerState) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def _position(player: PlayerState) -> Vec3:
    return Vec3(x=player.x, y=player.y, z=player.z)


def _fps_stats(records: Sequence[TelemetryRecord]) -> FpsStats | None:
    """Min and median frame rate, leaving out infinite readings (``1 / dt`` with ``dt = 0``)."""
    samples = [record.state.fps for record in records if math.isfinite(record.state.fps)]
    if not samples:
        return None
    return FpsStats(min=min(samples), median=statistics.median(samples))


def _collected_ids(records: Sequence[TelemetryRecord]) -> set[str]:
    """Ids with a ``collect`` event, or flagged ``collected`` in any snapshot."""
    collected = {
        event.id for _, event in _events(records, GameEventType.COLLECT) if event.id is not None
    }
    collected.update(
        collectible.id
        for record in records
        for collectible in record.state.collectibles
        if collectible.collected
    )
    return collected
