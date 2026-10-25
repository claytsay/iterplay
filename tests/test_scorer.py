"""The Scorer turns a version's evaluation into ``score.json`` (HLD §4 [7])."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from iterplay.contract.state import Vec3
from iterplay.evaluation.scorer import DEFAULT_MAX_DEATHS, DEFAULT_MIN_FPS, Scorer, ScoringInputs
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport, RunEnd
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.scoring import ItemScore, ScoreCard
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport
from iterplay.models.telemetry import (
    BotTelemetrySummary,
    FpsStats,
    OutOfBounds,
    TelemetrySummary,
    TimedPosition,
)
from iterplay.models.verification import VerifiedCritique

RUBRIC_FILE = Path(__file__).resolve().parents[1] / "rubric.yaml"
RANDOM, SPEEDRUNNER, NEWBIE = BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE
ORIGIN = Vec3(x=0, y=0)


def item(
    item_id: str,
    source: RubricSource = RubricSource.TELEMETRY,
    *,
    weight: float = 1,
    params: Mapping[str, float] | None = None,
) -> RubricItem:
    return RubricItem(
        id=item_id,
        description=f"The {item_id} item.",
        source=source,
        weight=weight,
        params=dict(params or {}),
    )


def smoke(*failed: SmokeCheck) -> SmokeReport:
    checks = [SmokeCheckResult(check=check, passed=check not in failed) for check in SmokeCheck]
    return SmokeReport(passed=not failed, checks=checks, attempt=1)


def playtest(
    uncaught: Mapping[BotName, Sequence[str]] | None = None,
    console: Mapping[BotName, Sequence[str]] | None = None,
) -> PlaytestReport:
    uncaught, console = uncaught or {}, console or {}
    runs = [
        BotRunResult(
            bot=bot,
            bot_seed=1,
            ended_by=RunEnd.TIME_LIMIT,
            duration_s=60,
            uncaught_errors=list(uncaught.get(bot, [])),
            console_errors=list(console.get(bot, [])),
        )
        for bot in BotName
    ]
    return PlaytestReport(game_seed=1, runs=runs)


def bot(
    name: BotName,
    *,
    completed: bool = False,
    deaths: int = 0,
    oob: Sequence[bool] = (),
    fps_min: float | None = 60.0,
    collected: Sequence[str] = (),
) -> BotTelemetrySummary:
    """One bot's digest; ``oob`` lists the intentional flag of each out-of-bounds event."""
    return BotTelemetrySummary(
        bot=name,
        completed=completed,
        time_to_complete_s=30.0 if completed else None,
        deaths=[TimedPosition(video_t=float(n), position=ORIGIN) for n in range(deaths)],
        out_of_bounds=[OutOfBounds(video_t=5, position=ORIGIN, intentional=flag) for flag in oob],
        stuck_intervals=[],
        fps=None if fps_min is None else FpsStats(min=fps_min, median=60),
        collected_ids=list(collected),
    )


def telemetry(*bots: BotTelemetrySummary, never_collected: Sequence[str] = ()) -> TelemetrySummary:
    return TelemetrySummary(bots=list(bots), never_collected_ids=list(never_collected))


GOOD = ScoringInputs(
    smoke=smoke(),
    playtest=playtest(),
    telemetry=telemetry(
        bot(RANDOM, collected=["a"]),
        bot(SPEEDRUNNER, completed=True, collected=["b"]),
        bot(NEWBIE, completed=True, deaths=1),
    ),
    verified=VerifiedCritique(
        issues=[], rubric_assessment={"matches_design_doc": 5, "game_feel": 5}
    ),
    headless=False,
)
"""A headed run of a version that does everything right."""


def shipped_rubric() -> Rubric:
    return Rubric.model_validate(yaml.safe_load(RUBRIC_FILE.read_text(encoding="utf-8")))


def score_of(rubric_item: RubricItem, inputs: ScoringInputs) -> ItemScore:
    [scored] = Scorer(Rubric(target=0.9, items=[rubric_item])).score(1, inputs).items
    return scored


def with_bots(*bots: BotTelemetrySummary, never_collected: Sequence[str] = ()) -> ScoringInputs:
    return replace(GOOD, telemetry=telemetry(*bots, never_collected=never_collected))


def test_a_flawless_version_scores_full_marks_on_the_shipped_rubric() -> None:
    rubric = shipped_rubric()

    card = Scorer(rubric).score(3, GOOD)

    assert (card.version, card.target) == (3, rubric.target)
    assert [(i.item_id, i.source, i.weight) for i in card.items] == [
        (i.id, i.source, i.weight) for i in rubric.items
    ]
    assert all(scored.score == 1.0 and scored.detail for scored in card.items)
    assert (card.total, card.meets_target) == (1.0, True)


def test_boots_cleanly_needs_every_smoke_check_to_pass() -> None:
    boots = item("boots_cleanly", RubricSource.SMOKE)

    assert score_of(boots, GOOD).score == 1.0
    failed = score_of(boots, replace(GOOD, smoke=smoke(SmokeCheck.READY, SmokeCheck.NO_ERRORS)))
    assert (failed.score, failed.detail) == (
        0.0,
        "Failed smoke checks on attempt 1: ready, no_errors.",
    )


def test_any_uncaught_error_fails_no_runtime_errors() -> None:
    errors = {RANDOM: ["TypeError: a", "TypeError: a"], NEWBIE: ["RangeError: b"]}

    scored = score_of(item("no_runtime_errors"), replace(GOOD, playtest=playtest(errors)))

    assert (scored.score, scored.detail) == (0.0, "3 uncaught errors: random 2, newbie 1.")


def test_console_errors_alone_do_not_fail_no_runtime_errors() -> None:
    noisy = replace(GOOD, playtest=playtest(console={RANDOM: ["404 favicon.ico"]}))

    scored = score_of(item("no_runtime_errors"), noisy)

    assert (scored.score, scored.detail) == (1.0, "No uncaught errors in 3 runs.")


@pytest.mark.parametrize(
    ("bots", "score", "detail"),
    [
        (
            [bot(SPEEDRUNNER, completed=True), bot(NEWBIE)],
            1.0,
            "The speedrunner completed the level in 30.0 s.",
        ),
        (
            [bot(SPEEDRUNNER), bot(NEWBIE, completed=True)],
            0.0,
            "The speedrunner did not complete the level.",
        ),
        (
            [bot(RANDOM), bot(NEWBIE, completed=True)],
            1.0,
            "No speedrunner run; completed by newbie.",
        ),
        (
            [bot(RANDOM), bot(NEWBIE)],
            0.0,
            "No speedrunner run, and no bot completed the level.",
        ),
    ],
)
def test_completable_follows_the_speedrunner_or_any_bot_without_one(
    bots: list[BotTelemetrySummary], score: float, detail: str
) -> None:
    scored = score_of(item("completable"), with_bots(*bots))

    assert (scored.score, scored.detail) == (score, detail)


@pytest.mark.parametrize(
    ("newbie", "params", "score"),
    [
        (bot(NEWBIE, completed=True, deaths=9), {}, 1.0),
        (bot(NEWBIE, deaths=DEFAULT_MAX_DEATHS), {}, 1.0),
        (bot(NEWBIE, deaths=DEFAULT_MAX_DEATHS + 1), {}, 0.0),
        (bot(NEWBIE, deaths=4), {"max_deaths": 5}, 1.0),
        (bot(NEWBIE, deaths=1), {"max_deaths": 0}, 0.0),
    ],
)
def test_fair_difficulty_lets_the_newbie_finish_or_die_at_most_max_deaths(
    newbie: BotTelemetrySummary, params: dict[str, float], score: float
) -> None:
    scored = score_of(item("fair_difficulty", params=params), with_bots(bot(SPEEDRUNNER), newbie))

    assert scored.score == score


def test_fair_difficulty_detail_counts_the_deaths() -> None:
    scored = score_of(item("fair_difficulty"), with_bots(bot(NEWBIE, deaths=4)))

    assert scored.detail == "The newbie did not finish and died 4 times; at most 3 allowed."


def test_fair_difficulty_is_unmeasured_without_a_newbie_run() -> None:
    scored = score_of(item("fair_difficulty"), with_bots(bot(SPEEDRUNNER, completed=True)))

    assert (scored.score, scored.detail) == (None, "No newbie run.")


@pytest.mark.parametrize(
    ("oob", "score", "detail"),
    [
        ((), 1.0, "No unintentional oob events."),
        ((True, True), 1.0, "No unintentional oob events (2 intentional pit falls ignored)."),
        (
            (True, False),
            0.0,
            "1 unintentional oob event, the first by the speedrunner bot at 5.0 s "
            "(1 intentional pit fall ignored).",
        ),
    ],
)
def test_collision_integrity_ignores_falls_into_intentional_pits(
    oob: tuple[bool, ...], score: float, detail: str
) -> None:
    scored = score_of(
        item("collision_integrity"), with_bots(bot(RANDOM), bot(SPEEDRUNNER, oob=oob))
    )

    assert (scored.score, scored.detail) == (score, detail)


@pytest.mark.parametrize(
    ("inputs", "score", "detail"),
    [
        (
            with_bots(
                bot(RANDOM, collected=["a"]), bot(NEWBIE, collected=["b"]), never_collected=["c"]
            ),
            0.0,
            "1 of 3 collectibles never collected: c.",
        ),
        (
            with_bots(bot(RANDOM, collected=["a"]), bot(NEWBIE, collected=["a", "b"])),
            1.0,
            "Bots collected every collectible (2 in all).",
        ),
        (with_bots(bot(RANDOM), bot(NEWBIE)), 1.0, "The level has no collectibles."),
    ],
)
def test_collectibles_reachable_needs_every_collectible_collected(
    inputs: ScoringInputs, score: float, detail: str
) -> None:
    scored = score_of(item("collectibles_reachable"), inputs)

    assert (scored.score, scored.detail) == (score, detail)


def test_performance_is_not_measured_in_headless_runs() -> None:
    scored = score_of(item("performance"), replace(GOOD, headless=True))

    assert scored.score is None
    assert "headless" in scored.detail


@pytest.mark.parametrize(
    ("fps_min", "params", "score"),
    [
        (DEFAULT_MIN_FPS, {}, 1.0),
        (24.5, {}, 0.0),
        (45.0, {"min_fps": 50}, 0.0),
        (24.5, {"min_fps": 20}, 1.0),
    ],
)
def test_performance_needs_the_minimum_fps_of_every_bot(
    fps_min: float, params: dict[str, float], score: float
) -> None:
    inputs = with_bots(bot(RANDOM), bot(NEWBIE, fps_min=fps_min), bot(SPEEDRUNNER, fps_min=None))

    scored = score_of(item("performance", params=params), inputs)

    assert scored.score == score
    assert scored.detail.startswith(f"Minimum FPS {fps_min:.1f} (newbie)")


def test_performance_is_unmeasured_without_fps_samples() -> None:
    scored = score_of(item("performance"), with_bots(bot(RANDOM, fps_min=None)))

    assert (scored.score, scored.detail) == (None, "No FPS samples.")


def test_critic_items_map_the_verified_rating_onto_zero_to_one() -> None:
    verified = VerifiedCritique(issues=[], rubric_assessment={"goal_clarity": 3, "completable": 1})
    inputs = replace(GOOD, verified=verified)

    rated = score_of(item("goal_clarity", RubricSource.CRITIC), inputs)

    assert (rated.score, rated.detail) == (0.6, "The critic rated it 3/5.")
    assert score_of(item("completable", RubricSource.CRITIC), inputs).score == 0.2


def test_critic_items_are_unmeasured_without_a_rating() -> None:
    unrated = score_of(
        item("game_feel", RubricSource.CRITIC), replace(GOOD, verified=VerifiedCritique(issues=[]))
    )
    uncritiqued = score_of(item("game_feel", RubricSource.CRITIC), replace(GOOD, verified=None))

    assert (unrated.score, unrated.detail) == (None, "The critic did not rate 'game_feel'.")
    assert (uncritiqued.score, uncritiqued.detail) == (None, "No verified critique.")


def test_a_build_that_never_booted_scores_zero_on_every_item() -> None:
    never_booted = ScoringInputs(
        smoke=smoke(SmokeCheck.READY), playtest=None, telemetry=None, verified=None, headless=False
    )

    card = Scorer(shipped_rubric()).score(2, never_booted)

    assert all(scored.score == 0.0 for scored in card.items)
    assert {scored.detail for scored in card.items if scored.item_id != "boots_cleanly"} == {
        "The build never booted."
    }
    assert (card.total, card.meets_target) == (0.0, False)


def test_a_booted_build_without_a_telemetry_summary_leaves_telemetry_items_unmeasured() -> None:
    rubric = Rubric(
        target=0.9,
        items=[
            item("completable"),
            item("no_runtime_errors"),
            item("game_feel", RubricSource.CRITIC),
        ],
    )

    card = Scorer(rubric).score(1, replace(GOOD, telemetry=None))

    assert [scored.score for scored in card.items] == [None, 1.0, 1.0]
    assert card.total == 1.0


def test_an_objective_item_without_a_measure_is_unmeasured() -> None:
    scored = score_of(item("speed"), GOOD)

    assert (scored.score, scored.detail) == (None, "No objective measure for 'speed'.")


def test_the_total_is_the_weighted_mean_of_the_measured_items() -> None:
    rubric = Rubric(
        target=0.5,
        items=[
            item("completable", weight=3),
            item("collision_integrity", weight=1),
            item("performance", weight=6),
        ],
    )
    inputs = replace(with_bots(bot(SPEEDRUNNER, completed=True, oob=[False])), headless=True)

    card = Scorer(rubric).score(1, inputs)

    assert [scored.score for scored in card.items] == [1.0, 0.0, None]
    assert card.total == 0.75


@pytest.mark.parametrize(("target", "meets_target"), [(0.75, True), (0.76, False)])
def test_meets_target_once_the_total_reaches_it(target: float, meets_target: bool) -> None:
    rubric = Rubric(
        target=target, items=[item("completable", weight=3), item("collision_integrity")]
    )
    inputs = with_bots(bot(SPEEDRUNNER, completed=True, oob=[False]))

    assert Scorer(rubric).score(1, inputs).meets_target is meets_target


def test_the_total_is_zero_when_nothing_was_measured() -> None:
    card = Scorer(Rubric(target=0.9, items=[item("performance")])).score(
        1, replace(GOOD, headless=True)
    )

    assert (card.items[0].score, card.total, card.meets_target) == (None, 0.0, False)


def test_score_card_round_trips_through_json() -> None:
    card = Scorer(shipped_rubric()).score(1, replace(GOOD, headless=True))

    assert ScoreCard.model_validate_json(card.model_dump_json()) == card
