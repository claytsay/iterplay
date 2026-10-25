"""The Verifier checks critic claims against telemetry and adds what they missed (HLD §4 [6])."""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from iterplay.contract.state import GameEventType, GameState
from iterplay.evaluation.telemetry_summary import summarize
from iterplay.evaluation.verifier import Evidence, Verifier, VerifierConfig
from iterplay.models.critique import Critique, EvidenceType, Issue, Severity
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport, RunEnd
from iterplay.models.telemetry import TelemetryRecord
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)

RANDOM, SPEEDRUNNER, NEWBIE = BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE
CONFIRMED = VerificationStatus.CONFIRMED
UNVERIFIABLE = VerificationStatus.UNVERIFIABLE
CONTRADICTED = VerificationStatus.CONTRADICTED


def record(video_t: float, *, x: float = 0.0, y: float = 0.0, **state: Any) -> TelemetryRecord:
    snapshot = {
        "t": video_t,
        "player": {"x": x, "y": y, "vx": 1, "vy": 0, "grounded": True},
        "goal": {"x": 100, "y": 0},
        "collectibles": state.get("collectibles", []),
        "deaths": 0,
        "levelComplete": state.get("complete", False),
        "fps": state.get("fps", 60),
        "events": state.get("events", []),
    }
    return TelemetryRecord(video_t=video_t, state=GameState.model_validate(snapshot))


def running(until_s: float = 60.0, **state: Any) -> list[TelemetryRecord]:
    """A bot running right at one unit per second, sampled every 0.5 s: never stuck."""
    return [record(n / 2, x=n / 2, **state) for n in range(int(until_s * 2) + 1)]


def finished(at_s: float, **state: Any) -> list[TelemetryRecord]:
    """A run that reaches the goal ``at_s`` seconds in."""
    return [*running(at_s - 0.5, **state), record(at_s, x=at_s, complete=True, **state)]


def at(records: Sequence[TelemetryRecord], video_t: float, **state: Any) -> list[TelemetryRecord]:
    """``records`` with the snapshot at ``video_t`` replaced; x stays put unless given."""
    return [
        record(video_t, **{"x": old.state.player.x, **state}) if old.video_t == video_t else old
        for old in records
    ]


def standing(
    records: Sequence[TelemetryRecord], from_s: float, to_s: float
) -> list[TelemetryRecord]:
    """``records`` with the player standing still from ``from_s`` to ``to_s``."""
    x = next(old.state.player.x for old in records if old.video_t == from_s)
    return [record(old.video_t, x=x) if from_s <= old.video_t <= to_s else old for old in records]


def oob(*, intentional: bool = False) -> dict[str, Any]:
    return {"type": GameEventType.OOB.value, "t": 1.0, "intentional": intentional}


def death() -> dict[str, Any]:
    return {"type": GameEventType.DEATH.value, "t": 1.0}


def gem(gem_id: str, *, collected: bool = False) -> dict[str, Any]:
    return {"id": gem_id, "x": 3, "y": 2, "collected": collected}


def bot_run(
    bot: BotName,
    *,
    uncaught: Sequence[str] = (),
    console: Sequence[str] = (),
    ended_by: RunEnd = RunEnd.TIME_LIMIT,
    crash_detail: str | None = None,
) -> BotRunResult:
    return BotRunResult(
        bot=bot,
        bot_seed=7,
        ended_by=ended_by,
        duration_s=60,
        uncaught_errors=list(uncaught),
        console_errors=list(console),
        crash_detail=crash_detail,
    )


def evidence(
    telemetry: Mapping[BotName, Sequence[TelemetryRecord]], *runs: BotRunResult
) -> Evidence:
    """Evidence for ``telemetry``; every bot gets a clean run unless ``runs`` are given."""
    report = PlaytestReport(game_seed=1, runs=list(runs) or [bot_run(bot) for bot in telemetry])
    return Evidence(playtest=report, telemetry=telemetry, summary=summarize(telemetry))


def claim(
    rubric_item: str,
    *,
    bot: BotName | None = SPEEDRUNNER,
    timestamp: str | None = "00:42",
    issue_id: str = "I1",
) -> Issue:
    return Issue(
        id=issue_id,
        bot=bot,
        timestamp=timestamp,
        rubric_item=rubric_item,
        severity=Severity.HIGH,
        observation="Something looks wrong.",
        evidence_type=EvidenceType.VISUAL,
    )


def check(issue: Issue, proof: Evidence, config: VerifierConfig | None = None) -> VerifiedIssue:
    """The verdict on a single critic claim."""
    return Verifier(config).verify(Critique(issues=[issue]), proof).issues[0]


def found(proof: Evidence, config: VerifierConfig | None = None) -> list[VerifiedIssue]:
    """The deterministic issues for a critique that reported nothing."""
    verified = Verifier(config).verify(Critique(issues=[]), proof)
    assert all(issue.origin is IssueOrigin.DETERMINISTIC for issue in verified.issues)
    return verified.issues


def wrong_everywhere() -> Evidence:
    """A speedrunner run that corroborates a claim about any objective item at 00:42."""
    speedrunner = at(running(collectibles=[gem("a")]), 42.0, y=-21, fps=12, events=[oob(), death()])
    return evidence({SPEEDRUNNER: speedrunner}, bot_run(SPEEDRUNNER, console=["Boom"]))


def test_hld_collision_claim_is_confirmed_by_a_nearby_oob_event(
    hld_critique: dict[str, Any],
) -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 41.0, y=-20.5, events=[oob()])})

    verified = Verifier().verify(Critique.model_validate(hld_critique), proof)

    issue = verified.issues[0]
    assert (issue.issue.id, issue.origin, issue.status) == ("I3", IssueOrigin.CRITIC, CONFIRMED)
    assert issue.rationale == (
        "1 unintentional oob event in the speedrunner run within 2 s of 00:42: 00:41."
    )


def test_collision_claim_is_confirmed_by_a_fall_below_the_kill_plane() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 42.5, y=-25)})

    verdict = check(claim("collision_integrity"), proof)

    assert verdict.status is CONFIRMED
    assert "y = -25.0 at 00:42" in verdict.rationale


def test_collision_claim_without_a_fall_is_contradicted() -> None:
    verdict = check(claim("collision_integrity"), evidence({SPEEDRUNNER: running()}))

    assert verdict.status is CONTRADICTED
    assert verdict.rationale == (
        "No oob event or fall below y = -20 in the speedrunner run within 2 s of 00:42."
    )


@pytest.mark.parametrize(("tolerance_s", "status"), [(2.0, CONTRADICTED), (3.0, CONFIRMED)])
def test_collision_evidence_must_be_near_the_timestamp(
    tolerance_s: float, status: VerificationStatus
) -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 45.0, events=[oob()])})

    verdict = check(
        claim("collision_integrity"), proof, VerifierConfig(time_tolerance_s=tolerance_s)
    )

    assert verdict.status is status


def test_collision_evidence_must_come_from_the_claimed_bot() -> None:
    proof = evidence({RANDOM: at(running(), 42.0, events=[oob()]), SPEEDRUNNER: running()})

    assert check(claim("collision_integrity"), proof).status is CONTRADICTED
    by_anyone = check(claim("collision_integrity", bot=None), proof)
    assert by_anyone.status is CONFIRMED
    assert by_anyone.rationale.endswith("in the playtest within 2 s of 00:42: random 00:42.")


def test_a_claim_without_a_timestamp_is_checked_against_the_whole_run() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 5.0, events=[oob()])})

    assert check(claim("collision_integrity", timestamp=None), proof).status is CONFIRMED
    assert check(claim("collision_integrity"), proof).status is CONTRADICTED


def test_falls_into_intentional_pits_alone_leave_a_collision_claim_unverifiable() -> None:
    """The reference game reports a pit fall below the kill plane, with ``intentional``."""
    pit = at(running(), 42.0, y=-20.3, events=[oob(intentional=True), death()])

    verdict = check(claim("collision_integrity"), evidence({SPEEDRUNNER: pit}))

    assert verdict.status is UNVERIFIABLE
    assert verdict.rationale == (
        "Only falls into intentional pits in the speedrunner run within 2 s of 00:42: 00:42. "
        "The game reports them as designed."
    )


def test_an_unintentional_fall_confirms_a_collision_claim_despite_nearby_pits() -> None:
    falls = at(at(running(), 41.0, events=[oob(intentional=True)]), 43.0, events=[oob()])

    verdict = check(claim("collision_integrity"), evidence({SPEEDRUNNER: falls}))

    assert verdict.status is CONFIRMED
    assert verdict.rationale.endswith("within 2 s of 00:42: 00:43.")


def test_a_fall_no_oob_event_accounts_for_confirms_a_collision_claim() -> None:
    proof = evidence(
        {
            RANDOM: at(running(), 42.0, y=-21, events=[oob(intentional=True)]),
            NEWBIE: at(running(), 43.0, y=-30),
        }
    )

    verdict = check(claim("collision_integrity", bot=None), proof)

    assert verdict.status is CONFIRMED
    assert "without an oob event" in verdict.rationale
    assert verdict.rationale.endswith("reaching y = -30.0 at newbie 00:43.")


def test_the_kill_plane_is_configurable() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 42.0, y=-6)})

    assert check(claim("collision_integrity"), proof).status is CONTRADICTED
    config = VerifierConfig(kill_plane_y=-5)
    assert check(claim("collision_integrity"), proof, config).status is CONFIRMED


def test_a_claim_about_a_bot_without_telemetry_is_unverifiable() -> None:
    verdict = check(claim("collision_integrity", bot=NEWBIE), evidence({SPEEDRUNNER: running()}))

    assert verdict.status is UNVERIFIABLE
    assert verdict.rationale == "No telemetry for the newbie run within 2 s of 00:42."


def test_a_claim_about_a_moment_after_the_run_ended_is_unverifiable() -> None:
    proof = evidence({SPEEDRUNNER: finished(30.0)})

    assert check(claim("collision_integrity", timestamp="00:50"), proof).status is UNVERIFIABLE


def test_impossible_level_claim_is_contradicted_when_any_bot_completed() -> None:
    proof = evidence({SPEEDRUNNER: running(), NEWBIE: finished(40.0)})

    verdict = check(claim("completable"), proof)

    assert verdict.status is CONTRADICTED
    assert verdict.rationale == "Completed by newbie, the fastest in 40.0 s of game time."


def test_impossible_level_claim_is_confirmed_when_no_bot_completed() -> None:
    proof = evidence({SPEEDRUNNER: running(), NEWBIE: running()})

    verified = Verifier().verify(Critique(issues=[claim("finishable")]), proof)

    [verdict] = verified.issues
    assert verdict.status is CONFIRMED
    assert verdict.rationale == "No bot completed the level in 2 runs."


def test_a_level_no_bot_completed_gets_a_critical_issue_when_the_critic_missed_it() -> None:
    proof = evidence({SPEEDRUNNER: running(), NEWBIE: running(30.0)})

    [issue] = found(proof)

    assert issue.status is CONFIRMED
    assert (issue.issue.id, issue.issue.rubric_item) == ("D1", "completable")
    assert issue.issue.severity is Severity.CRITICAL
    assert issue.issue.observation == (
        "No bot completed the level. The closest any bot came to the goal was 40.0 units, "
        "at (60.0, 0.0) (speedrunner, 01:00)."
    )
    assert issue.rationale == "levelComplete stayed false throughout 2 runs."


def test_collectible_claim_is_confirmed_by_an_uncollected_collectible() -> None:
    gems = [gem("a", collected=True), gem("b")]

    verdict = check(
        claim("collectibles_reachable"), evidence({SPEEDRUNNER: running(collectibles=gems)})
    )

    assert verdict.status is CONFIRMED
    assert verdict.rationale == "1 of 2 collectibles never collected: b."


def test_collectible_claim_is_contradicted_when_every_collectible_was_collected() -> None:
    proof = evidence({SPEEDRUNNER: running(collectibles=[gem("a", collected=True)])})

    verdict = check(claim("collectibles_reachable"), proof)

    assert verdict.status is CONTRADICTED
    assert verdict.rationale == "Every collectible was collected (1 in all)."


def test_collectible_claim_is_contradicted_when_there_are_none() -> None:
    verdict = check(claim("collectibles_reachable"), evidence({SPEEDRUNNER: running()}))

    assert verdict.status is CONTRADICTED
    assert verdict.rationale == "The level has no collectibles."


def test_difficulty_claim_is_confirmed_by_nearby_deaths() -> None:
    newbie = at(at(running(), 41.0, events=[death()]), 43.5, events=[death()])

    verdict = check(claim("fair_difficulty", bot=NEWBIE), evidence({NEWBIE: newbie}))

    assert verdict.status is CONFIRMED
    assert verdict.rationale == "2 deaths in the newbie run within 2 s of 00:42: 00:41, 00:43."


def test_difficulty_claim_without_nearby_deaths_is_unverifiable() -> None:
    proof = evidence({NEWBIE: at(running(), 10.0, events=[death()])})

    assert check(claim("fair_difficulty", bot=NEWBIE), proof).status is UNVERIFIABLE


def test_stutter_claim_is_confirmed_by_an_fps_dip() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 43.0, fps=12.5)})

    verdict = check(claim("performance"), proof)

    assert verdict.status is CONFIRMED
    assert "FPS fell to 12.5, below 30, at 00:43" in verdict.rationale


def test_stutter_claim_with_steady_fps_nearby_is_contradicted() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 10.0, fps=12.5)})

    verdict = check(claim("performance"), proof)

    assert verdict.status is CONTRADICTED
    assert "the lowest was 60.0" in verdict.rationale


def test_the_stutter_threshold_is_configurable() -> None:
    proof = evidence({SPEEDRUNNER: at(running(), 42.0, fps=40)})

    assert check(claim("performance"), proof).status is CONTRADICTED
    assert check(claim("performance"), proof, VerifierConfig(min_fps=45)).status is CONFIRMED


def test_stutter_claim_without_fps_samples_nearby_is_unverifiable() -> None:
    proof = evidence({SPEEDRUNNER: finished(30.0)})

    assert check(claim("performance", timestamp="00:50"), proof).status is UNVERIFIABLE


@pytest.mark.parametrize(
    "run",
    [
        bot_run(SPEEDRUNNER, console=["Failed to load resource"]),
        bot_run(SPEEDRUNNER, uncaught=["TypeError: x is undefined"]),
        bot_run(SPEEDRUNNER, ended_by=RunEnd.CRASHED, crash_detail="Target crashed"),
        bot_run(SPEEDRUNNER, ended_by=RunEnd.CRASHED),
    ],
)
def test_error_claim_is_confirmed_by_errors_or_a_crash_in_that_run(run: BotRunResult) -> None:
    proof = evidence({SPEEDRUNNER: running()}, run)

    assert check(claim("no_runtime_errors"), proof).status is CONFIRMED


def test_error_rationale_counts_errors_and_quotes_the_first() -> None:
    run = bot_run(SPEEDRUNNER, uncaught=["TypeError: a"], console=["404 favicon.ico"])

    verdict = check(claim("no_runtime_errors"), evidence({SPEEDRUNNER: running()}, run))

    assert verdict.rationale == "2 errors logged in the speedrunner run, the first: 'TypeError: a'."


def test_error_claim_against_a_clean_run_is_contradicted() -> None:
    runs = (bot_run(SPEEDRUNNER), bot_run(RANDOM, uncaught=["TypeError: a"]))
    proof = evidence({SPEEDRUNNER: running(), RANDOM: running()}, *runs)

    verdict = check(claim("no_runtime_errors"), proof)

    assert verdict.status is CONTRADICTED
    assert verdict.rationale == "No uncaught or console errors in the speedrunner run."
    assert check(claim("no_runtime_errors", bot=None), proof).status is CONFIRMED


def test_error_claim_about_a_bot_that_did_not_run_is_unverifiable() -> None:
    proof = evidence({SPEEDRUNNER: running()}, bot_run(SPEEDRUNNER))

    verdict = check(claim("no_runtime_errors", bot=NEWBIE), proof)

    assert verdict.status is UNVERIFIABLE
    assert verdict.rationale == "The playtest has no newbie run to check."


@pytest.mark.parametrize(
    "item", ["matches_design_doc", "game_feel", "goal_clarity", "boots_cleanly"]
)
def test_claims_telemetry_cannot_settle_are_unverifiable(item: str) -> None:
    verdict = check(claim(item), wrong_everywhere())

    assert verdict.status is UNVERIFIABLE
    assert repr(item) in verdict.rationale


@pytest.mark.parametrize(
    ("name", "item"),
    [
        ("collisions", "collision_integrity"),
        ("collision", "collision_integrity"),
        ("difficulty", "fair_difficulty"),
        ("fps", "performance"),
        ("perf", "performance"),
        ("framerate", "performance"),
        ("errors", "no_runtime_errors"),
        ("crash", "no_runtime_errors"),
        ("completion", "completable"),
        ("finishable", "completable"),
        ("collectibles", "collectibles_reachable"),
        ("Collision Integrity", "collision_integrity"),
        (" FPS ", "performance"),
        ("fair-difficulty", "fair_difficulty"),
    ],
)
def test_aliases_are_checked_like_the_rubric_item_they_name(name: str, item: str) -> None:
    proof = wrong_everywhere()

    by_name, by_id = check(claim(name), proof), check(claim(item), proof)

    assert by_id.status is CONFIRMED
    assert (by_name.status, by_name.rationale) == (by_id.status, by_id.rationale)


def test_each_distinct_uncaught_error_becomes_one_critical_issue() -> None:
    proof = evidence(
        {RANDOM: running(), NEWBIE: finished(40.0)},
        bot_run(RANDOM, uncaught=["TypeError: a", "TypeError: a", "RangeError: b"], console=["x"]),
        bot_run(NEWBIE, uncaught=["  TypeError: a\n"]),
    )

    issues = found(proof)

    assert [(issue.issue.id, issue.issue.observation, issue.issue.bot) for issue in issues] == [
        ("D1", "Uncaught error: TypeError: a", None),
        ("D2", "Uncaught error: RangeError: b", RANDOM),
    ]
    assert issues[0].rationale == "Thrown uncaught 3 times across 2 runs (random, newbie)."
    for issue in issues:
        assert issue.status is CONFIRMED
        assert issue.issue.rubric_item == "no_runtime_errors"
        assert issue.issue.severity is Severity.CRITICAL
        assert issue.issue.evidence_type is EvidenceType.CONSOLE


def test_never_collected_collectibles_become_one_high_issue() -> None:
    gems = [gem("a", collected=True), gem("c"), gem("b")]

    [issue] = found(evidence({SPEEDRUNNER: finished(30.0, collectibles=gems)}))

    assert issue.issue.rubric_item == "collectibles_reachable"
    assert issue.issue.severity is Severity.HIGH
    assert issue.issue.observation == "No bot collected 2 of the 3 collectibles: b, c."
    assert issue.rationale == "No collect event or collected flag for them in 1 run."


def test_long_stuck_intervals_become_medium_issues() -> None:
    proof = evidence(
        {
            RANDOM: standing(running(), 30.0, 34.5),
            SPEEDRUNNER: standing(running(), 10.0, 16.5),
            NEWBIE: standing(finished(50.0), 20.0, 25.0),
        }
    )

    issues = found(proof)

    summary = [(i.issue.bot, i.issue.rubric_item, i.issue.timestamp) for i in issues]
    assert summary == [(SPEEDRUNNER, "completable", "00:10"), (NEWBIE, "fair_difficulty", "00:20")]
    assert all(issue.issue.severity is Severity.MEDIUM for issue in issues)
    assert issues[0].issue.observation == (
        "The speedrunner bot was stuck near (10.0, 0.0) for 6.5 s, from 00:10 to 00:16."
    )
    assert issues[1].rationale.endswith("the newbie bot still completed the level.")


def test_the_stuck_issue_threshold_is_configurable() -> None:
    proof = evidence({RANDOM: standing(running(), 30.0, 34.5), NEWBIE: finished(50.0)})

    assert found(proof) == []
    assert len(found(proof, VerifierConfig(stuck_issue_s=4.0))) == 1


def test_deterministic_issues_are_numbered_in_a_fixed_order() -> None:
    speedrunner = standing(running(collectibles=[gem("a")]), 10.0, 20.0)
    proof = evidence({SPEEDRUNNER: speedrunner}, bot_run(SPEEDRUNNER, uncaught=["TypeError: a"]))

    issues = found(proof)

    assert [(i.issue.id, i.issue.rubric_item, i.issue.severity) for i in issues] == [
        ("D1", "no_runtime_errors", Severity.CRITICAL),
        ("D2", "collectibles_reachable", Severity.HIGH),
        ("D3", "completable", Severity.MEDIUM),
        ("D4", "completable", Severity.CRITICAL),
    ]


def test_deterministic_ids_skip_ids_the_critic_used() -> None:
    critique = Critique(issues=[claim("game_feel", issue_id="D1")])

    verified = Verifier().verify(critique, evidence({SPEEDRUNNER: running()}))

    assert [(issue.issue.id, issue.origin) for issue in verified.issues] == [
        ("D1", IssueOrigin.CRITIC),
        ("D2", IssueOrigin.DETERMINISTIC),
    ]


def test_a_clean_playtest_adds_no_deterministic_issues() -> None:
    telemetry = {
        RANDOM: running(),
        SPEEDRUNNER: finished(30.0, collectibles=[gem("a", collected=True)]),
        NEWBIE: finished(45.0),
    }

    assert found(evidence(telemetry)) == []


def test_verify_keeps_feel_notes_assessment_and_contradicted_claims(
    hld_critique: dict[str, Any],
) -> None:
    critique = Critique.model_validate(hld_critique)
    proof = evidence({SPEEDRUNNER: running(), NEWBIE: finished(50.0)})

    verified = Verifier().verify(critique, proof)

    assert verified.feel_notes == ["Jump arc looks floaty vs design doc 'snappy' target"]
    assert verified.rubric_assessment == {"goal_clarity": 2, "feel": 3}
    [i3] = verified.issues
    assert (i3.issue, i3.origin, i3.status) == (
        critique.issues[0],
        IssueOrigin.CRITIC,
        CONTRADICTED,
    )


def test_critic_issues_keep_their_order_ahead_of_deterministic_ones() -> None:
    critique = Critique(
        issues=[claim("game_feel", issue_id="I2"), claim("collisions", issue_id="I1")]
    )

    verified = Verifier().verify(critique, wrong_everywhere())

    assert [issue.issue.id for issue in verified.issues][:3] == ["I2", "I1", "D1"]


def test_verified_critique_round_trips_through_json(hld_critique: dict[str, Any]) -> None:
    verified = Verifier().verify(Critique.model_validate(hld_critique), wrong_everywhere())

    assert VerifiedCritique.model_validate_json(verified.model_dump_json()) == verified
