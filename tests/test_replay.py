"""Replay mode (HLD §6) steps through a finished run using only the artifacts on disk."""

import json
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import pytest

from iterplay.errors import RunNotFoundError
from iterplay.models.critique import EvidenceType, Issue, Severity
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.run import (
    RunConfig,
    RunSummary,
    StopReason,
    TokenUsage,
    VersionOutcome,
    VersionRecord,
)
from iterplay.models.scoring import ItemScore, ScoreCard
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)
from iterplay.replay import Replayer, ReplayStep
from iterplay.storage.artifacts import write_model, write_text
from iterplay.storage.layout import RunPaths
from iterplay.storage.run_store import RunStore

RUN_ID: Final = "replay-run"

RUBRIC: Final = Rubric(
    target=0.9,
    items=[
        RubricItem(
            id="completable", description="Completes.", source=RubricSource.TELEMETRY, weight=1
        )
    ],
)


@pytest.fixture
def run(tmp_path: Path) -> RunPaths:
    config = RunConfig(
        run_id=RUN_ID,
        created_at=datetime(2026, 10, 25, tzinfo=UTC),
        design_doc_sha256="0" * 64,
        rubric=RUBRIC,
        model="gemini-4-argon",
        bot_seeds={bot: seed for seed, bot in enumerate(BotName, start=1)},
    )
    return RunStore(tmp_path / "runs").create(config, "# Design doc\n")


def record(
    version: int, score: float = 0.5, outcome: VersionOutcome = VersionOutcome.ACCEPTED
) -> VersionRecord:
    return VersionRecord(
        version=version,
        parent_version=None if version == 1 else version - 1,
        score=score,
        outcome=outcome,
        duration_s=60,
        token_usage=TokenUsage(input_tokens=10, output_tokens=20),
    )


def score_card(version: int, total: float = 0.5) -> ScoreCard:
    item = ItemScore(item_id="completable", source=RubricSource.TELEMETRY, weight=1, score=total)
    return ScoreCard(
        version=version,
        items=[item],
        total=total,
        target=RUBRIC.target,
        meets_target=total >= RUBRIC.target,
    )


def critique(issue_id: str) -> VerifiedCritique:
    issue = Issue(
        id=issue_id,
        bot=BotName.SPEEDRUNNER,
        timestamp="00:42",
        rubric_item="collision_integrity",
        severity=Severity.HIGH,
        observation="The player falls through the floor.",
        evidence_type=EvidenceType.VISUAL_TELEMETRY,
    )
    verified = VerifiedIssue(
        issue=issue,
        origin=IssueOrigin.CRITIC,
        status=VerificationStatus.CONFIRMED,
        rationale="An oob event at 00:42.",
    )
    return VerifiedCritique(issues=[verified], feel_notes=["Floaty."])


def write_summary(run: RunPaths, records: list[VersionRecord]) -> None:
    summary = RunSummary(
        run_id=run.run_id,
        versions=records,
        best_version=max((r.version for r in records), default=None),
        stop_reason=StopReason.MAX_ITERATIONS,
    )
    write_model(run.summary, summary)


def write_versions(run: RunPaths, records: list[VersionRecord]) -> None:
    """``summary.json`` listing ``records``, and a ``score.json`` for each of them."""
    write_summary(run, records)
    for entry in records:
        write_model(run.version(entry.version).score, score_card(entry.version, entry.score))


def write_video(run: RunPaths, version: int, bot: BotName) -> Path:
    path = run.version(version).raw_video(bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"webm")
    return path


# Order


def test_steps_follow_the_summary(run: RunPaths) -> None:
    records = [record(1, 0.35), record(2, 0.6, VersionOutcome.REVERTED), record(3, 0.92)]
    write_versions(run, records)

    steps = list(Replayer(run).steps())

    assert [step.record for step in steps] == records
    assert [step.score for step in steps] == [
        score_card(1, 0.35),
        score_card(2, 0.6),
        score_card(3, 0.92),
    ]


def test_steps_keep_the_summary_order_rather_than_sorting(run: RunPaths) -> None:
    write_versions(run, [record(2), record(1)])

    assert [step.record.version for step in Replayer(run).steps()] == [2, 1]


def test_a_summary_without_versions_has_no_steps(run: RunPaths) -> None:
    write_summary(run, [])

    assert list(Replayer(run).steps()) == []


# Artifacts


def test_a_step_carries_every_artifact_of_its_version(run: RunPaths) -> None:
    write_versions(run, [record(1), record(2)])
    v2 = run.version(2)
    write_model(v2.critique_verified, critique("I3"))
    write_text(v2.changelog, "- I3: register the wall colliders\n")
    videos = {bot: write_video(run, 2, bot) for bot in BotName}

    step = list(Replayer(run).steps())[1]

    assert step == ReplayStep(
        record=record(2),
        score=score_card(2),
        verified=critique("I3"),
        changelog="- I3: register the wall colliders\n",
        videos=videos,
    )
    assert list(step.videos) == [BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE]


def test_optional_artifacts_default_to_empty(run: RunPaths) -> None:
    write_versions(run, [record(1, 0.0, VersionOutcome.BUILD_FAILED)])

    (step,) = Replayer(run).steps()

    assert step.verified is None
    assert step.changelog == ""
    assert step.videos == {}


def test_only_recordings_that_exist_are_listed(run: RunPaths) -> None:
    write_versions(run, [record(1)])
    random = write_video(run, 1, BotName.RANDOM)
    newbie = write_video(run, 1, BotName.NEWBIE)
    run.version(1).raw_video(BotName.SPEEDRUNNER).mkdir()
    write_text(run.version(1).combined_video, "not a raw recording")

    (step,) = Replayer(run).steps()

    assert step.videos == {BotName.RANDOM: random, BotName.NEWBIE: newbie}


def test_the_videos_mapping_is_read_only(run: RunPaths) -> None:
    write_versions(run, [record(1)])
    (step,) = Replayer(run).steps()
    videos = cast(MutableMapping[BotName, Path], step.videos)

    with pytest.raises(TypeError):
        videos[BotName.RANDOM] = run.root / "elsewhere.webm"


def test_the_changelog_is_read_as_utf8_with_bad_bytes_replaced(run: RunPaths) -> None:
    write_versions(run, [record(1), record(2)])
    write_text(run.version(1).changelog, "- I1: café — ☃\n")
    run.version(2).changelog.write_bytes(b"- I2: \xff broken\n")

    steps = list(Replayer(run).steps())

    assert steps[0].changelog == "- I1: café — ☃\n"
    assert steps[1].changelog == "- I2: � broken\n"


# Laziness and freshness


def test_versions_are_read_as_iteration_reaches_them(run: RunPaths) -> None:
    write_versions(run, [record(1), record(2)])
    steps = Replayer(run).steps()
    write_model(run.version(1).critique_verified, critique("I1"))

    first = next(steps)
    write_text(run.version(2).changelog, "- I1: fixed\n")
    second = next(steps)

    assert first.verified == critique("I1")
    assert second.changelog == "- I1: fixed\n"
    assert next(steps, None) is None


def test_each_call_reads_the_run_again(run: RunPaths) -> None:
    write_versions(run, [record(1)])
    replayer = Replayer(run)
    assert len(list(replayer.steps())) == 1

    write_versions(run, [record(1), record(2)])

    assert len(list(replayer.steps())) == 2


def test_a_run_opened_by_the_store_replays(run: RunPaths, tmp_path: Path) -> None:
    write_versions(run, [record(1)])

    opened = RunStore(tmp_path / "runs").open(run.root)

    assert [step.record for step in Replayer(opened).steps()] == [record(1)]


# Missing and invalid artifacts


def test_a_missing_summary_raises_before_iteration(run: RunPaths) -> None:
    with pytest.raises(RunNotFoundError, match=r"it has no summary\.json") as raised:
        Replayer(run).steps()

    assert str(run.root) in str(raised.value)


def test_a_missing_run_directory_raises(tmp_path: Path) -> None:
    run = RunPaths(tmp_path / "runs" / "nope")

    with pytest.raises(RunNotFoundError, match="the directory does not exist"):
        Replayer(run).steps()


def test_a_directory_that_is_not_a_run_raises(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()

    with pytest.raises(RunNotFoundError, match=r"it is not a run, because it has no config\.json"):
        Replayer(RunPaths(tmp_path / "photos")).steps()


def test_a_missing_score_raises_naming_the_file_after_earlier_steps(run: RunPaths) -> None:
    write_summary(run, [record(1), record(2)])
    write_model(run.version(1).score, score_card(1))
    steps = Replayer(run).steps()

    assert next(steps).record == record(1)
    with pytest.raises(RunNotFoundError) as raised:
        next(steps)

    message = str(raised.value)
    assert str(run.version(2).score) in message
    assert message.startswith(f"Cannot replay v2 of {RUN_ID}:")


def test_a_corrupt_summary_raises_a_value_error_naming_it(run: RunPaths) -> None:
    write_text(run.summary, "{not json")

    with pytest.raises(ValueError, match=r"is not a valid RunSummary: Invalid JSON") as raised:
        Replayer(run).steps()

    assert str(run.summary) in str(raised.value)


def test_an_invalid_score_raises_a_value_error_naming_the_field(run: RunPaths) -> None:
    write_summary(run, [record(1)])
    payload = score_card(1).model_dump(mode="json") | {"total": 2}
    write_text(run.version(1).score, json.dumps(payload))
    steps = Replayer(run).steps()

    with pytest.raises(ValueError, match=r"is not a valid ScoreCard: total: ") as raised:
        next(steps)

    message = str(raised.value)
    assert message.startswith(str(run.version(1).score))
    assert message.endswith("total: Input should be less than or equal to 1")


def test_every_problem_in_an_artifact_is_counted(run: RunPaths) -> None:
    write_summary(run, [record(1)])
    write_text(run.version(1).score, "{}")

    with pytest.raises(ValueError, match=r"version: Field required \(and 4 more\)$"):
        list(Replayer(run).steps())


def test_an_invalid_optional_artifact_still_raises(run: RunPaths) -> None:
    write_versions(run, [record(1)])
    write_text(run.version(1).critique_verified, '{"issues": "none"}')

    with pytest.raises(ValueError, match=r"verified\.json is not a valid VerifiedCritique: issues"):
        list(Replayer(run).steps())
