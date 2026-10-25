"""The telemetry digest given to the critic and Scorer (HLD §4 [5])."""

from collections.abc import Sequence
from typing import Any

import pytest

from iterplay.contract.state import GameEventType, GameState, Vec3
from iterplay.evaluation.telemetry_summary import SummaryConfig, summarize, summarize_bot
from iterplay.models.playtest import BotName
from iterplay.models.telemetry import (
    FpsStats,
    OutOfBounds,
    StuckInterval,
    TelemetryRecord,
    TelemetrySummary,
    TimedPosition,
)

CONFIG = SummaryConfig()


def snapshot(
    *,
    t: float = 0.0,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    complete: bool = False,
    fps: float = 60.0,
    events: Sequence[dict[str, Any]] = (),
    collectibles: Sequence[dict[str, Any]] = (),
) -> GameState:
    return GameState.model_validate(
        {
            "t": t,
            "player": {"x": x, "y": y, "z": z, "vx": 0, "vy": 0, "grounded": True},
            "goal": {"x": 50, "y": 0},
            "collectibles": list(collectibles),
            "deaths": 0,
            "levelComplete": complete,
            "fps": fps,
            "events": list(events),
        }
    )


def record(video_t: float, **state: Any) -> TelemetryRecord:
    """A snapshot taken ``video_t`` seconds into the recording; game time defaults to it."""
    state.setdefault("t", video_t)
    return TelemetryRecord(video_t=video_t, state=snapshot(**state))


def positions(*xs: float, step: float = 0.5, **state: Any) -> list[TelemetryRecord]:
    """One snapshot per x position, ``step`` seconds apart from video time 0."""
    return [record(index * step, x=x, **state) for index, x in enumerate(xs)]


def event(kind: GameEventType, t: float = 0.0, **extra: Any) -> dict[str, Any]:
    return {"type": kind.value, "t": t, **extra}


def gem(gem_id: str, *, collected: bool = False) -> dict[str, Any]:
    return {"id": gem_id, "x": 1, "y": 1, "collected": collected}


def test_empty_run_reports_nothing() -> None:
    summary = summarize_bot(BotName.RANDOM, [], CONFIG)

    assert summary.bot is BotName.RANDOM
    assert summary.completed is False
    assert summary.time_to_complete_s is None
    assert (summary.deaths, summary.out_of_bounds, summary.stuck_intervals) == ([], [], [])
    assert summary.fps is None
    assert summary.collected_ids == []


def test_completion_time_is_the_game_time_of_the_first_complete_snapshot() -> None:
    records = [
        record(1.0, t=0.8, x=0),
        record(2.0, t=1.6, x=5, complete=True),
        record(3.0, t=2.4, x=5, complete=True),
    ]

    summary = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG)

    assert summary.completed is True
    assert summary.time_to_complete_s == 1.6


def test_an_earlier_complete_event_sets_the_completion_time() -> None:
    records = [
        record(1.0, x=0),
        record(2.0, t=5.0, x=5, complete=True, events=[event(GameEventType.COMPLETE, 4.75)]),
    ]

    assert summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).time_to_complete_s == 4.75


def test_a_complete_event_alone_is_not_completion() -> None:
    records = [record(1.0, events=[event(GameEventType.COMPLETE, 1.0)])]

    summary = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG)

    assert summary.completed is False
    assert summary.time_to_complete_s is None


def test_deaths_are_dated_by_video_time_and_placed_by_the_event() -> None:
    death = event(GameEventType.DEATH, 9.4, x=3, y=-1.5, cause="spikes")
    records = [record(0.0, x=0), record(12.0, t=9.5, x=1, y=2, events=[death])]

    deaths = summarize_bot(BotName.NEWBIE, records, CONFIG).deaths

    assert deaths == [TimedPosition(video_t=12.0, position=Vec3(x=3, y=-1.5))]


def test_a_death_without_coordinates_is_placed_at_the_player() -> None:
    records = [record(4.0, x=7, y=2, z=-1, events=[event(GameEventType.DEATH)])]

    deaths = summarize_bot(BotName.NEWBIE, records, CONFIG).deaths

    assert deaths == [TimedPosition(video_t=4.0, position=Vec3(x=7, y=2, z=-1))]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"x": 3}, (3, 2, 0)),
        ({"z": 4}, (7, 2, 4)),
        ({"x": "4.5", "y": "-1"}, (4.5, -1, 0)),
        ({"x": "left", "y": None}, (7, 2, 0)),
        ({"x": True, "y": [1]}, (7, 2, 0)),
        ({"x": "nan", "y": "Infinity"}, (7, 2, 0)),
    ],
)
def test_event_coordinates_fall_back_to_the_player_one_by_one(
    extra: dict[str, Any], expected: tuple[float, float, float]
) -> None:
    records = [record(1.0, x=7, y=2, events=[event(GameEventType.DEATH, **extra)])]

    position = summarize_bot(BotName.NEWBIE, records, CONFIG).deaths[0].position

    assert (position.x, position.y, position.z) == expected


def test_every_event_is_counted_once() -> None:
    records = [
        record(1.0, events=[event(GameEventType.DEATH), event(GameEventType.DEATH)]),
        record(1.5),
        record(2.0, events=[event(GameEventType.DEATH)]),
    ]

    deaths = summarize_bot(BotName.RANDOM, records, CONFIG).deaths

    assert [death.video_t for death in deaths] == [1.0, 1.0, 2.0]


def test_oob_events_keep_their_intentional_flag() -> None:
    pit = event(GameEventType.OOB, 3.0, intentional=True)
    clip = event(GameEventType.OOB, 6.0)
    records = [record(3.5, x=10, y=-21, events=[pit]), record(7.0, x=20, y=-22, events=[clip])]

    falls = summarize_bot(BotName.RANDOM, records, CONFIG).out_of_bounds

    assert falls == [
        OutOfBounds(video_t=3.5, position=Vec3(x=10, y=-21), intentional=True),
        OutOfBounds(video_t=7.0, position=Vec3(x=20, y=-22), intentional=False),
    ]


def test_a_fall_reports_both_its_oob_event_and_its_death() -> None:
    """The reference game emits ``oob`` and then ``death`` (with x/y) in the same frame."""
    fall = [
        event(GameEventType.OOB, 8.0, intentional=False),
        event(GameEventType.DEATH, 8.0, cause="fell", x=31.5, y=0.2),
    ]
    records = [record(8.1, x=31.4, y=-20.4, events=fall)]

    summary = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG)

    assert summary.out_of_bounds[0].position == Vec3(x=31.4, y=-20.4)
    assert summary.deaths[0].position == Vec3(x=31.5, y=0.2)


def test_standing_still_long_enough_is_a_stuck_interval() -> None:
    records = positions(2, 2, 2, 2, 5, 9)

    stuck = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).stuck_intervals

    assert stuck == [StuckInterval(start_video_t=0.0, end_video_t=1.5, position=Vec3(x=2, y=0))]


def test_a_short_pause_is_not_stuck() -> None:
    records = positions(0, 1, 1, 2, 3, 4)

    assert summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).stuck_intervals == []


def test_jitter_within_the_radius_counts_as_stuck() -> None:
    records = positions(0, 0.2, -0.2, 0.1, -0.25, 3)

    stuck = summarize_bot(BotName.RANDOM, records, CONFIG).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(0.0, 2.0)]


def test_drift_is_measured_from_the_start_of_the_stretch() -> None:
    """Each step is within the radius of the last, but 0.4 is too far from where it began."""
    records = positions(0, 0.2, 0.4, 0.4, 0.4)

    stuck = summarize_bot(BotName.RANDOM, records, CONFIG).stuck_intervals

    assert stuck == [StuckInterval(start_video_t=1.0, end_video_t=2.0, position=Vec3(x=0.4, y=0))]


def test_movement_in_depth_counts_for_3d_games() -> None:
    records = [record(index * 0.5, z=index) for index in range(4)]

    assert summarize_bot(BotName.RANDOM, records, CONFIG).stuck_intervals == []


@pytest.mark.parametrize("kind", [GameEventType.DEATH, GameEventType.RESPAWN])
def test_a_death_or_respawn_starts_a_new_stretch(kind: GameEventType) -> None:
    records = positions(1, 1, 1, 1, 1, 1, 1)
    records[3] = record(1.5, x=1, events=[event(kind, 1.5)])

    stuck = summarize_bot(BotName.NEWBIE, records, CONFIG).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(0.0, 1.0), (1.5, 3.0)]


@pytest.mark.parametrize("kind", [GameEventType.COLLECT, GameEventType.OOB])
def test_other_events_do_not_split_a_stretch(kind: GameEventType) -> None:
    records = positions(1, 1, 1, 1, 1)
    records[2] = record(1.0, x=1, events=[event(kind, 1.0, id="gem-1")])

    stuck = summarize_bot(BotName.NEWBIE, records, CONFIG).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(0.0, 2.0)]


def test_waiting_after_completion_is_not_stuck() -> None:
    records = positions(4, 4, 4, 4, 4, 4, 4)
    records[3:] = [record(r.video_t, x=4, complete=True) for r in records[3:]]

    stuck = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(0.0, 1.0)]


def test_stuck_until_the_run_ends_is_reported() -> None:
    records = positions(0, 3, 6, 6, 6, 6, 6)

    stuck = summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(1.0, 3.0)]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (SummaryConfig(), []),
        (SummaryConfig(stuck_radius=1.0), [(0.0, 1.5)]),
        (SummaryConfig(stuck_min_s=0.5), [(0.0, 0.5), (1.0, 1.5)]),
    ],
)
def test_stuck_detection_follows_the_config(
    config: SummaryConfig, expected: list[tuple[float, float]]
) -> None:
    records = positions(0, 0.2, 0.5, 0.6, 3)

    stuck = summarize_bot(BotName.RANDOM, records, config).stuck_intervals

    assert [(s.start_video_t, s.end_video_t) for s in stuck] == expected


def test_fps_reports_the_minimum_and_median() -> None:
    records = [record(index * 0.1, fps=fps) for index, fps in enumerate([60, 20.5, 50, 58])]

    assert summarize_bot(BotName.RANDOM, records, CONFIG).fps == FpsStats(min=20.5, median=54)


def with_fps(item: TelemetryRecord, fps: float) -> TelemetryRecord:
    """A copy whose FPS bypasses validation, which rejects non-finite numbers."""
    return item.model_copy(update={"state": item.state.model_copy(update={"fps": fps})})


def test_infinite_fps_readings_are_ignored() -> None:
    records = [record(0.0), record(0.1, fps=30), record(0.2, fps=40)]
    records[0] = with_fps(records[0], float("inf"))

    assert summarize_bot(BotName.RANDOM, records, CONFIG).fps == FpsStats(min=30, median=35)


def test_no_finite_fps_reading_means_no_fps_stats() -> None:
    records = [with_fps(record(0.0), float("inf"))]

    assert summarize_bot(BotName.RANDOM, records, CONFIG).fps is None


def test_collected_ids_come_from_collect_events_and_flags() -> None:
    gems = [gem("b", collected=True), gem("c"), gem("a")]
    records = [
        record(1.0, collectibles=gems, events=[event(GameEventType.COLLECT, id="a")]),
        record(2.0, events=[event(GameEventType.COLLECT, id=7), event(GameEventType.COLLECT)]),
    ]

    assert summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).collected_ids == ["7", "a", "b"]


def test_a_collectible_counts_once_it_was_ever_flagged_collected() -> None:
    records = [
        record(1.0, collectibles=[gem("a", collected=True)]),
        record(2.0, collectibles=[gem("a", collected=False)]),
    ]

    assert summarize_bot(BotName.SPEEDRUNNER, records, CONFIG).collected_ids == ["a"]


def test_summary_lists_bots_in_clip_order() -> None:
    telemetry = {
        BotName.NEWBIE: positions(0, 1),
        BotName.RANDOM: positions(0, 1),
        BotName.SPEEDRUNNER: positions(0, 1),
    }

    summary = summarize(telemetry)

    assert [bot.bot for bot in summary.bots] == [
        BotName.RANDOM,
        BotName.SPEEDRUNNER,
        BotName.NEWBIE,
    ]


def test_never_collected_ids_are_those_no_bot_collected() -> None:
    telemetry = {
        BotName.RANDOM: [record(1.0, events=[event(GameEventType.COLLECT, id="a")])],
        BotName.SPEEDRUNNER: [
            record(1.0, collectibles=[gem("a"), gem("c"), gem("b", collected=True)]),
        ],
        BotName.NEWBIE: [record(1.0, collectibles=[gem("d"), gem("a")])],
    }

    summary = summarize(telemetry)

    assert summary.never_collected_ids == ["c", "d"]
    assert [bot.collected_ids for bot in summary.bots] == [["a"], ["b"], []]


def test_summary_uses_the_default_config_unless_given_one() -> None:
    telemetry = {BotName.RANDOM: positions(0, 0, 3)}

    assert summarize(telemetry).bots[0].stuck_intervals == []
    stuck = summarize(telemetry, SummaryConfig(stuck_min_s=0.5)).bots[0].stuck_intervals
    assert [(s.start_video_t, s.end_video_t) for s in stuck] == [(0.0, 0.5)]


def test_summary_of_no_runs_is_empty() -> None:
    assert summarize({}) == TelemetrySummary(bots=[], never_collected_ids=[])


def test_summary_round_trips_through_json() -> None:
    death = event(GameEventType.DEATH, 2.0, x=1, y=2)
    telemetry = {
        BotName.SPEEDRUNNER: [
            *positions(0, 0, 0, collectibles=[gem("a")]),
            record(1.5, x=4, events=[death, event(GameEventType.OOB, 2.0, intentional=True)]),
            record(2.0, x=9, complete=True, fps=31.5),
        ]
    }
    summary = summarize(telemetry)

    assert TelemetrySummary.model_validate_json(summary.model_dump_json()) == summary
