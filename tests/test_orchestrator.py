"""The iteration loop (HLD §2, §4 [7]): build, boot with quick fixes, evaluate, keep the best
version, stop, fix, and persist every artifact of HLD §6 along the way.

Most tests run the real agents, cache, meter, Verifier, Scorer, storage and game server, with
scripted model replies and stand-ins for the browser and ffmpeg stages. The stand-ins fetch
the game they are served and judge it by markers in its ``main.js``. The tests marked
``browser`` and ``ffmpeg`` run every real stage on the fixture games.
"""

import asyncio
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from iterplay.agents.builder import Builder
from iterplay.agents.critic.direct import DirectCalls
from iterplay.agents.fixer import AttemptSummary, Fixer, FixRequest, FixResult
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.contract.controls import CONTROLS_2D
from iterplay.contract.layout import THREE_MODULE_PATH, VENDOR_DIR
from iterplay.contract.state import GameState
from iterplay.contract.templates import DEFAULT_TEMPLATE, TEMPLATES
from iterplay.errors import BudgetExceededError, LLMError, MediaError
from iterplay.evaluation.rubric import load_rubric
from iterplay.evaluation.scorer import Scorer
from iterplay.evaluation.verifier import Verifier
from iterplay.harness.browser import BrowserOptions, HarnessBrowser
from iterplay.harness.playtest import PlaytestConfig, PlaytestRunner
from iterplay.harness.server import game_server
from iterplay.harness.smoke import SmokeConfig, SmokeTester
from iterplay.llm.base import LLMClient
from iterplay.llm.cache import CachingLLMClient, ResponseCache
from iterplay.llm.metering import MeteredLLMClient, UsageMeter
from iterplay.media.ffmpeg import FFmpeg
from iterplay.media.prep import MediaPreparer
from iterplay.models.critique import Critique
from iterplay.models.game import (
    BuilderResponse,
    ChangelogEntry,
    FixerResponse,
    GameBundle,
    GameFile,
)
from iterplay.models.media import PreparedMedia
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport, RunEnd
from iterplay.models.rubric import Rubric, RubricSource
from iterplay.models.run import (
    Budgets,
    ModelPricing,
    RunConfig,
    RunSummary,
    StopReason,
    TokenUsage,
    VersionOutcome,
)
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport
from iterplay.models.telemetry import TelemetryRecord, TelemetrySummary
from iterplay.models.verification import IssueOrigin, VerificationStatus, VerifiedCritique
from iterplay.orchestrator.loop import MAX_QUICK_RETRIES, Agents, Orchestrator, Stages
from iterplay.orchestrator.policy import KeepBestPolicy, StopPolicy
from iterplay.serving import LoopbackHTTPServer
from iterplay.storage.artifacts import read_model, write_model, write_text
from iterplay.storage.game_files import read_game
from iterplay.storage.layout import RunPaths, VersionPaths
from iterplay.storage.run_store import RunStore
from iterplay.storage.telemetry import TelemetryWriter, read_telemetry
from tests.helpers import FakeLLMClient, Reply

REPO = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "games"
MODEL = "test-model"
DESIGN_DOC = "# Gem dash\n\nRun right to the flag, collecting both gems on the way.\n"
BOT_SEEDS = {BotName.RANDOM: 1, BotName.SPEEDRUNNER: 2, BotName.NEWBIE: 3}
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"

RUBRIC = load_rubric(REPO / "rubric.yaml")
PERFECTIONIST = Rubric(target=1.0, items=RUBRIC.items)
"""The default rubric, except that only a perfect score stops the run early."""

BROKEN: Final = "BROKEN"
"""Marks a stand-in game that fails its smoke test, like a build without the harness API."""

UNFINISHABLE: Final = "UNFINISHABLE"
"""Marks a stand-in game whose level no bot can complete."""

RANDOM_CONSOLE_ERROR: Final = "it broke (main.js:3)"
"""Logged by the stand-in playtest's random bot."""

SMOKE_ISSUE_IDS: Final = ["B1", "B2", "B3"]
"""A build that never becomes ready fails three smoke checks: ready, and the two that need it."""


# Scripted model replies.


def stand_in_game(*markers: str) -> list[GameFile]:
    """A game for the stand-in stages, which judge it by the markers in its ``main.js``."""
    return [
        GameFile(
            path="index.html",
            content='<!doctype html>\n<script type="module" src="./main.js"></script>\n',
        ),
        GameFile(
            path="main.js",
            content=f'// {" ".join(markers) or "fine"}\nwindow.iterplay = {{ version: "1" }};\n',
        ),
    ]


def fixture_game(name: str, replacements: Mapping[str, str] | None = None) -> list[GameFile]:
    """A fixture game's files, with each ``old: new`` replacement made in its ``main.js``."""
    files = []
    for path in sorted((FIXTURES / name).iterdir()):
        content = path.read_text(encoding="utf-8")
        if path.name == "main.js":
            for old, new in (replacements or {}).items():
                assert old in content, old
                content = content.replace(old, new)
        files.append(GameFile(path=path.name, content=content))
    return files


def bundle(files: Sequence[GameFile]) -> GameBundle:
    return GameBundle(files=sorted(files, key=lambda file: file.path))


def built(files: Sequence[GameFile]) -> BuilderResponse:
    return BuilderResponse(files=list(files))


def fixed(files: Sequence[GameFile], *issue_ids: str, summary: str = "Fixed it") -> FixerResponse:
    entry = ChangelogEntry(issue_ids=list(issue_ids or ("I1",)), summary=summary)
    return FixerResponse(files=list(files), changelog=[entry])


def critique(rating: int = 1) -> Critique:
    """A subjective claim the Verifier cannot settle, and the same rating for both critic
    items."""
    return Critique.model_validate(
        {
            "issues": [
                {
                    "id": "I1",
                    "bot": "speedrunner",
                    "timestamp": "00:01",
                    "rubric_item": "game_feel",
                    "severity": "low",
                    "observation": "Jumps feel floaty.",
                    "evidence_type": "visual",
                }
            ],
            "feel_notes": ["Floaty jumps"],
            "rubric_assessment": {"matches_design_doc": rating, "game_feel": rating},
        }
    )


def hallucinating_critique(rating: int = 3) -> Critique:
    """:func:`critique`, plus a fall through the floor that never happened."""
    claim = {
        "id": "I2",
        "bot": "speedrunner",
        "timestamp": "00:02",
        "rubric_item": "collisions",
        "severity": "high",
        "observation": "The player falls through the floor.",
        "evidence_type": "visual",
    }
    base = critique(rating).model_dump(mode="json")
    return Critique.model_validate({**base, "issues": [*base["issues"], claim]})


# Stand-ins for the browser and ffmpeg stages.


def serve_on_any_port(game_dir: Path) -> LoopbackHTTPServer:
    return game_server(game_dir, port=0)


async def served_script(base_url: str) -> str:
    """The ``main.js`` the game server serves, fetched over loopback HTTP."""
    return await asyncio.to_thread(_fetch, f"{base_url}main.js")


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def smoke_report(*, passed: bool, attempt: int) -> SmokeReport:
    if passed:
        checks = [SmokeCheckResult(check=check, passed=True) for check in SmokeCheck]
    else:
        skipped = "skipped: the game never became ready."
        checks = [
            SmokeCheckResult(
                check=SmokeCheck.READY,
                passed=False,
                detail="window.iterplay.ready did not become true within 10 s",
            ),
            SmokeCheckResult(check=SmokeCheck.STATE_SCHEMA, passed=False, detail=skipped),
            SmokeCheckResult(check=SmokeCheck.NO_ERRORS, passed=True),
            SmokeCheckResult(check=SmokeCheck.PLAYER_MOVES, passed=False, detail=skipped),
        ]
    return SmokeReport(passed=passed, checks=checks, attempt=attempt)


class StandInSmoke(SmokeTester):
    """Fails a game marked :data:`BROKEN` at the ready check."""

    def __init__(self) -> None:
        self.attempts: list[int] = []

    async def run(self, base_url: str, *, attempt: int = 1) -> SmokeReport:
        self.attempts.append(attempt)
        broken = BROKEN in await served_script(base_url)
        return smoke_report(passed=not broken, attempt=attempt)


def snapshots(*, finishable: bool) -> list[TelemetryRecord]:
    """Ten snapshots 0.5 s apart. The player runs right, collecting g1 at x = 3, and reaches
    the goal at x = 9, unless the level is unfinishable: then it stops at x = 4."""
    records = []
    for step in range(10):
        complete = finishable and step == 9
        events: list[dict[str, object]] = []
        if step == 3:
            events.append({"type": "collect", "t": step / 2, "id": "g1"})
        if complete:
            events.append({"type": "complete", "t": step / 2})
        state = GameState.model_validate(
            {
                "t": step / 2,
                "player": {
                    "x": step if finishable else min(step, 4),
                    "y": 0.5,
                    "vx": 2,
                    "vy": 0,
                    "grounded": True,
                },
                "goal": {"x": 9, "y": 0.5},
                "collectibles": [{"id": "g1", "x": 3, "y": 0.5, "collected": step >= 3}],
                "deaths": 0,
                "levelComplete": complete,
                "fps": 60,
                "events": events,
            }
        )
        records.append(TelemetryRecord(video_t=step / 2, state=state))
    return records


def record_run(paths: VersionPaths, bot: BotName, seed: int, *, finishable: bool) -> BotRunResult:
    records = snapshots(finishable=finishable)
    with TelemetryWriter(paths.telemetry(bot)) as telemetry:
        for record in records:
            telemetry.append(record)
    return BotRunResult(
        bot=bot,
        bot_seed=seed,
        ended_by=RunEnd.LEVEL_COMPLETE if finishable else RunEnd.TIME_LIMIT,
        duration_s=records[-1].video_t,
        console_errors=[RANDOM_CONSOLE_ERROR] if bot is BotName.RANDOM else [],
    )


class StandInPlaytest(PlaytestRunner):
    """Records scripted telemetry for every bot, listing the bots in reverse clip order.

    Nobody completes a level marked :data:`UNFINISHABLE`. ``on_run`` is called first with
    the version's paths, so tests can look at the run mid-loop or move a fake clock.
    """

    def __init__(self, on_run: Callable[[VersionPaths], None] | None = None) -> None:
        self.on_run = on_run
        self.calls: list[tuple[int, dict[BotName, int]]] = []

    async def run(
        self,
        base_url: str,
        *,
        game_seed: int,
        bot_seeds: Mapping[BotName, int],
        paths: VersionPaths,
    ) -> PlaytestReport:
        self.calls.append((game_seed, dict(bot_seeds)))
        if self.on_run is not None:
            self.on_run(paths)
        finishable = UNFINISHABLE not in await served_script(base_url)
        runs = [
            record_run(paths, bot, bot_seeds[bot], finishable=finishable)
            for bot in reversed(BotName)
        ]
        report = PlaytestReport(game_seed=game_seed, runs=runs)
        write_text(paths.console_log, "")
        write_model(paths.playtest_report, report)
        return report


def write_placeholder_clips(paths: VersionPaths, bots: Sequence[BotName]) -> PreparedMedia:
    per_bot = {}
    for bot in bots:
        clip = paths.prepared_video(bot)
        clip.write_bytes(f"{bot} clip".encode())
        per_bot[bot] = clip
    paths.combined_video.write_bytes(b"combined clip")
    return PreparedMedia(combined=paths.combined_video, per_bot=per_bot)


class StandInMedia(MediaPreparer):
    """Writes placeholder clips, or fails as ffmpeg does when no recording can be used."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.bots: list[list[BotName]] = []

    async def prepare(self, paths: VersionPaths, bots: Sequence[BotName]) -> PreparedMedia:
        self.bots.append(list(bots))
        if self.fail:
            raise MediaError("no clip could be prepared:\nrandom: no recording")
        return write_placeholder_clips(paths, bots)


@dataclass
class StandIns:
    smoke: StandInSmoke = field(default_factory=StandInSmoke)
    playtest: StandInPlaytest = field(default_factory=StandInPlaytest)
    media: StandInMedia = field(default_factory=StandInMedia)

    def stages(self, rubric: Rubric = PERFECTIONIST) -> Stages:
        return Stages(
            smoke=self.smoke,
            playtest=self.playtest,
            media=self.media,
            verifier=Verifier(),
            scorer=Scorer(rubric),
            serve_game=serve_on_any_port,
        )


# Wiring, as iterplay.app does it.


class SpyFixer(Fixer):
    """A Fixer that records every request it is given."""

    def __init__(self, llm: LLMClient, prompts: PromptLibrary, requests: list[FixRequest]) -> None:
        super().__init__(llm, prompts)
        self._requests = requests

    async def fix(self, request: FixRequest) -> FixResult:
        self._requests.append(request)
        return await super().fix(request)


class ExhaustedAfter(UsageMeter):
    """A meter whose token budget runs out once ``calls`` model calls are recorded."""

    def __init__(self, calls: int) -> None:
        super().__init__(Budgets(), None)
        self._calls_left = calls

    def record(self, usage: TokenUsage) -> None:
        super().record(usage)
        self._calls_left -= 1

    def exhausted(self) -> StopReason | None:
        return StopReason.TOKEN_BUDGET if self._calls_left <= 0 else None


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Loop:
    """An orchestrator wired as ``iterplay.app`` does it, and what it was asked to do."""

    orchestrator: Orchestrator
    run: RunPaths
    llm: FakeLLMClient
    meter: UsageMeter
    fixes: list[FixRequest]
    agents_made_for: list[VersionPaths]

    def version(self, number: int) -> VersionPaths:
        return self.run.version(number)


def make_loop(
    tmp_path: Path,
    replies: Mapping[str, Sequence[Reply]],
    stages: Stages | None = None,
    *,
    rubric: Rubric = PERFECTIONIST,
    budgets: Budgets | None = None,
    pricing: ModelPricing | None = None,
    meter: UsageMeter | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Loop:
    budgets = budgets or Budgets(max_iterations=3)
    config = RunConfig(
        run_id="test-run",
        created_at=datetime.now(UTC),
        design_doc_sha256="0" * 64,
        rubric=rubric,
        model=MODEL,
        bot_seeds=dict(BOT_SEEDS),
        budgets=budgets,
        pricing=pricing,
    )
    run = RunStore(tmp_path / "runs").create(config, DESIGN_DOC)
    llm = FakeLLMClient(replies)
    meter = meter or UsageMeter(budgets, pricing)
    prompts = PromptLibrary(TEMPLATES[DEFAULT_TEMPLATE])
    metered = MeteredLLMClient(llm, meter)
    fixes: list[FixRequest] = []
    agents_made_for: list[VersionPaths] = []

    def agents(paths: VersionPaths) -> Agents:
        agents_made_for.append(paths)
        client = CachingLLMClient(metered, ResponseCache(paths.llm_dir, MODEL))
        return Agents(
            builder=Builder(client, prompts),
            critic=DirectCalls(client, prompts),
            fixer=SpyFixer(client, prompts, fixes),
        )

    orchestrator = Orchestrator(
        config=config,
        run=run,
        design_doc=DESIGN_DOC,
        agents=agents,
        stages=stages or StandIns().stages(rubric),
        keep_best=KeepBestPolicy(),
        stop=StopPolicy(target=rubric.target, budgets=budgets, meter=meter, clock=clock),
        meter=meter,
        clock=clock,
    )
    return Loop(orchestrator, run, llm, meter, fixes, agents_made_for)


def outcomes(summary: RunSummary) -> list[VersionOutcome]:
    return [record.outcome for record in summary.versions]


def cached_purposes(paths: VersionPaths) -> list[str]:
    """The purposes of the model calls cached in a version's ``llm/``, sorted."""
    return sorted(
        json.loads(entry.read_text(encoding="utf-8"))["request"]["purpose"]
        for entry in paths.llm_dir.glob("*.json")
    )


def issue_ids(request: FixRequest) -> list[str]:
    return [verified.issue.id for verified in request.issues]


# One version, start to finish.


async def test_v1_is_built_booted_evaluated_and_recorded(tmp_path: Path) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    v1 = loop.version(1)
    assert read_game(v1.game_dir) == bundle(stand_in_game())
    assert (v1.game_dir / THREE_MODULE_PATH).is_file()
    assert (v1.game_dir / VENDOR_DIR / "LICENSE").is_file()
    smoke = read_model(v1.smoke_report, SmokeReport)
    assert (smoke.passed, smoke.attempt) == (True, 1)
    telemetry = read_model(v1.telemetry_summary, TelemetrySummary)
    assert [bot.bot for bot in telemetry.bots] == list(BotName)
    assert all(bot.completed for bot in telemetry.bots)
    assert read_model(v1.critique_raw, Critique) == critique()
    verified = read_model(v1.critique_verified, VerifiedCritique)
    assert [(item.issue.id, item.status) for item in verified.issues] == [
        ("I1", VerificationStatus.UNVERIFIABLE)
    ]
    score = read_model(v1.score, ScoreCard)
    assert score.version == 1
    assert score.total == pytest.approx(79 / 95)
    assert v1.changelog.read_text(encoding="utf-8") == ""

    assert read_model(loop.run.summary, RunSummary) == summary
    assert summary.run_id == "test-run"
    assert summary.stop_reason is StopReason.MAX_ITERATIONS
    assert summary.best_version == 1
    (record,) = summary.versions
    assert (record.version, record.parent_version) == (1, None)
    assert (record.outcome, record.score) == (VersionOutcome.ACCEPTED, score.total)
    assert [request.purpose for request in loop.llm.requests] == ["builder", "critic"]
    assert cached_purposes(v1) == ["builder", "critic"]
    assert loop.agents_made_for == [v1]


async def test_the_critic_watches_the_combined_clip_in_clip_order(tmp_path: Path) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    await loop.orchestrator.run()

    assert stand_ins.media.bots == [list(BotName)]
    (request,) = loop.llm.requests_for("critic")
    assert [media.path for media in request.media] == [loop.version(1).combined_video]
    assert RANDOM_CONSOLE_ERROR in request.prompt
    assert DESIGN_DOC.strip() in request.prompt


async def test_bots_play_the_run_seeds(tmp_path: Path) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    await loop.orchestrator.run()

    assert stand_ins.playtest.calls == [(1, BOT_SEEDS)]
    report = read_model(loop.version(1).playtest_report, PlaytestReport)
    assert {run.bot: run.bot_seed for run in report.runs} == BOT_SEEDS


# Fixing, keep-best and reverts.


async def test_each_version_is_fixed_from_the_one_before_while_none_regresses(
    tmp_path: Path,
) -> None:
    v1, v2, v3 = stand_in_game("v1"), stand_in_game("v2"), stand_in_game("v3")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(v1)],
            "critic": [critique()] * 3,
            "fixer": [fixed(v2, "I1", summary="Snappier jumps"), fixed(v3, "I1")],
        },
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [VersionOutcome.ACCEPTED] * 3
    assert [record.parent_version for record in summary.versions] == [None, 1, 2]
    assert summary.best_version == 3
    assert summary.stop_reason is StopReason.MAX_ITERATIONS
    first, second = loop.fixes
    assert first.design_doc == DESIGN_DOC
    assert first.current == bundle(v1)
    assert issue_ids(first) == ["I1"]
    assert first.rejected_attempt is None
    assert first.history == (
        AttemptSummary(
            version=1,
            parent_version=None,
            score=summary.versions[0].score,
            score_delta=None,
            outcome=VersionOutcome.ACCEPTED,
            changes=(),
        ),
    )
    assert second.current == bundle(v2)
    assert second.rejected_attempt is None
    assert [attempt.version for attempt in second.history] == [1, 2]
    v2_attempt = second.history[1]
    assert (v2_attempt.parent_version, v2_attempt.score_delta) == (1, 0.0)
    assert v2_attempt.changes == (ChangelogEntry(issue_ids=["I1"], summary="Snappier jumps"),)
    for number, files in enumerate((v1, v2, v3), start=1):
        assert read_game(loop.version(number).game_dir) == bundle(files)


async def test_every_version_gets_its_own_agents_and_llm_cache(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game("v1"))],
            "critic": [critique()] * 3,
            "fixer": [fixed(stand_in_game("v2")), fixed(stand_in_game("v3"))],
        },
    )

    await loop.orchestrator.run()

    assert loop.agents_made_for == [loop.version(number) for number in (1, 2, 3)]
    assert cached_purposes(loop.version(1)) == ["builder", "critic"]
    assert cached_purposes(loop.version(2)) == ["critic", "fixer"]
    assert cached_purposes(loop.version(3)) == ["critic", "fixer"]


async def test_a_regression_is_reverted_and_the_next_fix_starts_from_the_best(
    tmp_path: Path,
) -> None:
    v1, worse, v3 = stand_in_game("v1"), stand_in_game(UNFINISHABLE), stand_in_game("v3")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(v1)],
            "critic": [critique()] * 3,
            "fixer": [fixed(worse, "I1", summary="Raise the goal"), fixed(v3, "I1")],
        },
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [
        VersionOutcome.ACCEPTED,
        VersionOutcome.REVERTED,
        VersionOutcome.ACCEPTED,
    ]
    first, second, third = summary.versions
    assert second.score < first.score
    assert (second.parent_version, third.parent_version) == (1, 1)
    assert summary.best_version == 3
    request = loop.fixes[1]
    assert request.current == bundle(v1)
    assert issue_ids(request) == ["I1"]
    assert [attempt.version for attempt in request.history] == [1, 2]
    assert request.history[1].score_delta == pytest.approx(second.score - first.score)
    rejected = request.rejected_attempt
    assert rejected is not None
    assert rejected.attempt == request.history[1]
    assert rejected.attempt.outcome is VersionOutcome.REVERTED
    assert rejected.attempt.changes == (ChangelogEntry(issue_ids=["I1"], summary="Raise the goal"),)
    assert [(item.issue.id, item.status) for item in rejected.issues] == [
        ("D1", VerificationStatus.CONFIRMED),
        ("I1", VerificationStatus.UNVERIFIABLE),
    ]
    assert read_game(loop.version(3).game_dir) == bundle(v3)


async def test_the_best_version_survives_a_later_regression(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game("v1"))],
            "critic": [critique()] * 2,
            "fixer": [fixed(stand_in_game(UNFINISHABLE))],
        },
        budgets=Budgets(max_iterations=2),
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [VersionOutcome.ACCEPTED, VersionOutcome.REVERTED]
    assert summary.best_version == 1


# Booting: the smoke test and quick fixes.


async def test_a_broken_build_is_repaired_by_a_quick_fix_in_the_same_version(
    tmp_path: Path,
) -> None:
    stand_ins = StandIns()
    broken, repaired = stand_in_game(BROKEN), stand_in_game("repaired")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(broken)],
            "critic": [critique()],
            "fixer": [fixed(repaired, "B1", summary="Define window.iterplay")],
        },
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    assert stand_ins.smoke.attempts == [1, 2]
    v1 = loop.version(1)
    smoke = read_model(v1.smoke_report, SmokeReport)
    assert (smoke.passed, smoke.attempt) == (True, 2)
    assert read_game(v1.game_dir) == bundle(repaired)
    (quick_fix,) = loop.fixes
    assert quick_fix.current == bundle(broken)
    assert quick_fix.history == ()
    assert quick_fix.rejected_attempt is None
    assert issue_ids(quick_fix) == SMOKE_ISSUE_IDS
    assert {item.origin for item in quick_fix.issues} == {IssueOrigin.SMOKE}
    assert all(item.issue.rubric_item == "boots_cleanly" for item in quick_fix.issues)
    assert v1.changelog.read_text(encoding="utf-8") == "- [B1] Define window.iterplay\n"
    (record,) = summary.versions
    assert (record.outcome, record.parent_version) == (VersionOutcome.ACCEPTED, None)
    assert cached_purposes(v1) == ["builder", "critic", "fixer"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def test_each_server_of_a_version_can_reuse_one_fixed_port(tmp_path: Path) -> None:
    """As with ``iterplay new`` on port 8737: every smoke attempt and the playtest get a
    fresh server on the same port, and none outlives its stage."""
    port = free_port()
    servers: list[LoopbackHTTPServer] = []

    def serve_on_the_fixed_port(game_dir: Path) -> LoopbackHTTPServer:
        servers.append(game_server(game_dir, port=port))
        return servers[-1]

    stages = replace(StandIns().stages(), serve_game=serve_on_the_fixed_port)
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game(BROKEN))],
            "critic": [critique()],
            "fixer": [fixed(stand_in_game("repaired"), "B1")],
        },
        stages,
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [VersionOutcome.ACCEPTED]
    assert [server.base_url for server in servers] == [f"http://127.0.0.1:{port}/"] * 3
    with pytest.raises(urllib.error.URLError):
        _fetch(f"http://127.0.0.1:{port}/main.js")


async def test_a_build_that_stays_broken_fails_after_every_quick_retry(tmp_path: Path) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game(BROKEN, "v1"))],
            "fixer": [
                fixed(stand_in_game(BROKEN, "retry 1"), "B1", summary="Retry once"),
                fixed(stand_in_game(BROKEN, "retry 2"), "B1", summary="Retry twice"),
            ],
        },
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    assert stand_ins.smoke.attempts == list(range(1, MAX_QUICK_RETRIES + 2))
    assert len(loop.fixes) == MAX_QUICK_RETRIES
    v1 = loop.version(1)
    smoke = read_model(v1.smoke_report, SmokeReport)
    assert (smoke.passed, smoke.attempt) == (False, MAX_QUICK_RETRIES + 1)
    assert read_game(v1.game_dir) == bundle(stand_in_game(BROKEN, "retry 2"))
    score = read_model(v1.score, ScoreCard)
    assert score.total == 0
    assert not v1.critique_verified.exists()
    assert not v1.critique_raw.exists()
    assert not v1.playtest_dir.exists()
    assert stand_ins.playtest.calls == []
    assert v1.changelog.read_text(encoding="utf-8") == "- [B1] Retry once\n- [B1] Retry twice\n"
    assert outcomes(summary) == [VersionOutcome.BUILD_FAILED]
    assert summary.best_version is None
    assert summary.stop_reason is StopReason.MAX_ITERATIONS


@pytest.mark.parametrize(
    "failure",
    [
        [LLMError("The model is unavailable")],
        [FixerResponse(files=stand_in_game()[:1], changelog=[])] * 2,
    ],
    ids=["model error", "files rejected twice"],
)
async def test_a_failed_quick_fix_ends_the_retries(tmp_path: Path, failure: list[Reply]) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game(BROKEN))], "fixer": failure},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    assert stand_ins.smoke.attempts == [1]
    assert outcomes(summary) == [VersionOutcome.BUILD_FAILED]
    assert read_model(loop.version(1).smoke_report, SmokeReport).attempt == 1


async def test_until_a_version_boots_each_fix_starts_from_the_latest_build(
    tmp_path: Path,
) -> None:
    last_retry, booting = stand_in_game(BROKEN, "retry 2"), stand_in_game("boots")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game(BROKEN, "v1"))],
            "critic": [critique()],
            "fixer": [
                fixed(stand_in_game(BROKEN, "retry 1"), "B1"),
                fixed(last_retry, "B1"),
                fixed(booting, "B1", summary="Set ready once loaded"),
            ],
        },
        budgets=Budgets(max_iterations=2),
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [VersionOutcome.BUILD_FAILED, VersionOutcome.ACCEPTED]
    assert summary.versions[1].parent_version == 1
    assert summary.best_version == 2
    request = loop.fixes[2]
    assert request.current == bundle(last_retry)
    assert issue_ids(request) == SMOKE_ISSUE_IDS
    assert request.rejected_attempt is None
    (attempt,) = request.history
    assert (attempt.version, attempt.outcome, attempt.score) == (
        1,
        VersionOutcome.BUILD_FAILED,
        0.0,
    )
    assert len(attempt.changes) == MAX_QUICK_RETRIES


async def test_a_build_failure_after_a_boot_is_rejected_with_its_smoke_issues(
    tmp_path: Path,
) -> None:
    v1 = stand_in_game("v1")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(v1)],
            "critic": [critique()] * 2,
            "fixer": [
                fixed(stand_in_game(BROKEN, "v2"), "I1"),
                LLMError("The model is unavailable"),
                fixed(stand_in_game("v3"), "B1"),
            ],
        },
    )

    summary = await loop.orchestrator.run()

    assert outcomes(summary) == [
        VersionOutcome.ACCEPTED,
        VersionOutcome.BUILD_FAILED,
        VersionOutcome.ACCEPTED,
    ]
    assert summary.versions[2].parent_version == 1
    request = loop.fixes[2]
    assert request.current == bundle(v1)
    assert issue_ids(request) == ["I1"]
    rejected = request.rejected_attempt
    assert rejected is not None
    assert (rejected.attempt.version, rejected.attempt.outcome) == (
        2,
        VersionOutcome.BUILD_FAILED,
    )
    assert [item.issue.id for item in rejected.issues] == SMOKE_ISSUE_IDS
    assert {item.origin for item in rejected.issues} == {IssueOrigin.SMOKE}


# Degraded evaluation.


async def test_a_critic_that_fails_leaves_only_the_deterministic_issues(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game(UNFINISHABLE))],
            "critic": ["this is not JSON", critique()],
            "fixer": [fixed(stand_in_game("v2"), "D1")],
        },
        budgets=Budgets(max_iterations=2),
    )

    summary = await loop.orchestrator.run()

    v1 = loop.version(1)
    assert not v1.critique_raw.exists()
    verified = read_model(v1.critique_verified, VerifiedCritique)
    assert [(item.issue.id, item.origin) for item in verified.issues] == [
        ("D1", IssueOrigin.DETERMINISTIC)
    ]
    score = read_model(v1.score, ScoreCard)
    critic_items = {
        item.item_id: item.score for item in score.items if item.source is RubricSource.CRITIC
    }
    assert critic_items == {"matches_design_doc": None, "game_feel": None}
    assert score.total == pytest.approx(55 / 75)
    assert outcomes(summary) == [VersionOutcome.ACCEPTED, VersionOutcome.ACCEPTED]
    assert issue_ids(loop.fixes[0]) == ["D1"]


async def test_without_a_clip_the_critic_is_skipped(tmp_path: Path) -> None:
    stand_ins = StandIns(media=StandInMedia(fail=True))
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game(UNFINISHABLE))]},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    assert loop.llm.requests_for("critic") == []
    v1 = loop.version(1)
    assert not v1.critique_raw.exists()
    verified = read_model(v1.critique_verified, VerifiedCritique)
    assert {item.origin for item in verified.issues} == {IssueOrigin.DETERMINISTIC}
    assert outcomes(summary) == [VersionOutcome.ACCEPTED]


# Stopping.


async def test_the_run_stops_once_the_target_is_reached(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique(5)]},
        rubric=RUBRIC,
        budgets=Budgets(max_iterations=5),
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is StopReason.TARGET_REACHED
    assert len(summary.versions) == 1
    assert read_model(loop.version(1).score, ScoreCard).meets_target
    assert loop.fixes == []


async def test_the_run_stops_when_the_wall_time_budget_is_spent(tmp_path: Path) -> None:
    clock = FakeClock()
    stand_ins = StandIns(playtest=StandInPlaytest(on_run=lambda _paths: clock.advance(10)))
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game("v1"))],
            "critic": [critique()] * 2,
            "fixer": [fixed(stand_in_game("v2"))],
        },
        stand_ins.stages(),
        budgets=Budgets(max_iterations=5, max_wall_time_s=15),
        clock=clock,
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is StopReason.TIME_BUDGET
    assert len(summary.versions) == 2


async def test_versions_are_timed_and_metered(tmp_path: Path) -> None:
    clock = FakeClock()
    stand_ins = StandIns(playtest=StandInPlaytest(on_run=lambda _paths: clock.advance(10)))
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game("v1"))],
            "critic": [critique()] * 2,
            "fixer": [fixed(stand_in_game("v2"))],
        },
        stand_ins.stages(),
        budgets=Budgets(max_iterations=2),
        pricing=ModelPricing(input_usd_per_mtok=1.0, output_usd_per_mtok=4.0),
        clock=clock,
    )

    summary = await loop.orchestrator.run()

    assert [record.duration_s for record in summary.versions] == [10.0, 10.0]
    assert summary.duration_s == 20.0
    assert summary.token_usage == loop.meter.total
    assert all(record.token_usage.input_tokens > 0 for record in summary.versions)
    assert all(record.token_usage.output_tokens > 0 for record in summary.versions)
    assert (
        sum(record.token_usage.input_tokens for record in summary.versions),
        sum(record.token_usage.output_tokens for record in summary.versions),
    ) == (summary.token_usage.input_tokens, summary.token_usage.output_tokens)
    assert summary.spend_usd == loop.meter.spend_usd
    assert summary.spend_usd is not None
    assert summary.spend_usd > 0


async def test_summary_json_is_rewritten_after_every_version(tmp_path: Path) -> None:
    seen: list[RunSummary | None] = []

    def peek(paths: VersionPaths) -> None:
        summary = paths.root.parent / "summary.json"
        seen.append(read_model(summary, RunSummary) if summary.exists() else None)

    stand_ins = StandIns(playtest=StandInPlaytest(on_run=peek))
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game("v1"))],
            "critic": [critique()] * 3,
            "fixer": [fixed(stand_in_game("v2")), fixed(stand_in_game("v3"))],
        },
        stand_ins.stages(),
    )

    final = await loop.orchestrator.run()

    first, after_v1, after_v2 = seen
    assert first is None
    assert after_v1 is not None
    assert after_v2 is not None
    assert [record.version for record in after_v1.versions] == [1]
    assert [record.version for record in after_v2.versions] == [1, 2]
    assert after_v1.stop_reason is None
    assert after_v2.stop_reason is None
    assert after_v2.versions == final.versions[:2]
    assert final.stop_reason is StopReason.MAX_ITERATIONS


async def test_the_stop_policy_ends_the_run_once_the_budget_is_spent(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        meter=ExhaustedAfter(calls=2),
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is StopReason.TOKEN_BUDGET
    assert outcomes(summary) == [VersionOutcome.ACCEPTED]
    assert loop.fixes == []


@pytest.mark.parametrize(
    ("budgets", "pricing", "reason"),
    [
        (Budgets(max_iterations=3, max_total_tokens=1), None, StopReason.TOKEN_BUDGET),
        (
            Budgets(max_iterations=3, max_spend_usd=1e-9),
            ModelPricing(input_usd_per_mtok=1.0, output_usd_per_mtok=1.0),
            StopReason.SPEND_BUDGET,
        ),
    ],
    ids=["tokens", "spend"],
)
async def test_a_budget_refusal_mid_version_stops_the_run_without_it(
    tmp_path: Path, budgets: Budgets, pricing: ModelPricing | None, reason: StopReason
) -> None:
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        budgets=budgets,
        pricing=pricing,
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is reason
    assert summary.versions == []
    assert summary.best_version is None
    assert read_model(loop.run.summary, RunSummary) == summary
    assert loop.llm.requests_for("critic") == []
    assert read_model(loop.version(1).smoke_report, SmokeReport).passed
    assert not loop.version(1).score.exists()


async def test_a_budget_refusal_during_a_quick_fix_stops_the_run(tmp_path: Path) -> None:
    stand_ins = StandIns()
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game(BROKEN))]},
        stand_ins.stages(),
        budgets=Budgets(max_iterations=3, max_total_tokens=1),
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is StopReason.TOKEN_BUDGET
    assert summary.versions == []
    assert stand_ins.smoke.attempts == [1]
    assert len(loop.fixes) == 1
    assert loop.llm.requests_for("fixer") == []
    assert not loop.version(1).score.exists()


async def test_a_budget_error_from_the_fixer_keeps_the_finished_versions(tmp_path: Path) -> None:
    refusal = BudgetExceededError("Budget exhausted; refusing the 'fixer' model call")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game())],
            "critic": [critique()],
            "fixer": [refusal],
        },
    )

    summary = await loop.orchestrator.run()

    assert summary.stop_reason is StopReason.TOKEN_BUDGET
    assert outcomes(summary) == [VersionOutcome.ACCEPTED]
    assert read_model(loop.run.summary, RunSummary) == summary


# Errors that end the run.


async def test_an_orchestrator_drives_a_single_run(tmp_path: Path) -> None:
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()]},
        budgets=Budgets(max_iterations=1),
    )
    summary = await loop.orchestrator.run()

    with pytest.raises(RuntimeError, match="already run"):
        await loop.orchestrator.run()

    assert read_model(loop.run.summary, RunSummary) == summary
    assert [request.purpose for request in loop.llm.requests] == ["builder", "critic"]


async def test_a_builder_failure_propagates_before_any_summary(tmp_path: Path) -> None:
    loop = make_loop(tmp_path, {"builder": [LLMError("The model is unavailable")]})

    with pytest.raises(LLMError, match="unavailable"):
        await loop.orchestrator.run()

    assert not loop.run.summary.exists()


async def test_a_fixer_failure_propagates_and_the_summary_keeps_earlier_versions(
    tmp_path: Path,
) -> None:
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(stand_in_game())],
            "critic": [critique()],
            "fixer": [LLMError("The model is unavailable")],
        },
    )

    with pytest.raises(LLMError, match="unavailable"):
        await loop.orchestrator.run()

    summary = read_model(loop.run.summary, RunSummary)
    assert outcomes(summary) == [VersionOutcome.ACCEPTED]
    assert summary.stop_reason is None


# Patch notes.


async def test_the_changelog_lists_every_fixer_note_as_a_markdown_bullet(tmp_path: Path) -> None:
    reply = FixerResponse(
        files=stand_in_game("v2"),
        changelog=[
            ChangelogEntry(issue_ids=["I1", " D1 "], summary="Make the\n  jumps snappier."),
            ChangelogEntry(issue_ids=["I2"], summary="Move the HUD to the top right"),
        ],
    )
    loop = make_loop(
        tmp_path,
        {"builder": [built(stand_in_game())], "critic": [critique()] * 2, "fixer": [reply]},
        budgets=Budgets(max_iterations=2),
    )

    await loop.orchestrator.run()

    assert loop.version(1).changelog.read_text(encoding="utf-8") == ""
    assert loop.version(2).changelog.read_text(encoding="utf-8") == (
        "- [I1, D1] Make the jumps snappier.\n- [I2] Move the HUD to the top right\n"
    )


# Every real stage, on the fixture games.


@pytest.fixture
async def browser() -> AsyncIterator[HarnessBrowser]:
    async with HarnessBrowser(BrowserOptions()) as harness:
        yield harness


def real_stages(browser: HarnessBrowser, rubric: Rubric = PERFECTIONIST) -> Stages:
    return Stages(
        smoke=SmokeTester(browser, CONTROLS_2D, SmokeConfig(ready_timeout_s=3.0, move_hold_s=1.0)),
        playtest=PlaytestRunner(browser, CONTROLS_2D, PlaytestConfig(max_duration_s=5.0)),
        media=MediaPreparer(FFmpeg()),
        verifier=Verifier(),
        scorer=Scorer(rubric),
        serve_game=serve_on_any_port,
    )


def assert_hld_layout(version: VersionPaths) -> None:
    """Every artifact HLD §6 lists for a version that booted, plus the playtest report,
    telemetry summary and per-bot clips."""
    for name in ("index.html", "main.js", THREE_MODULE_PATH, f"{VENDOR_DIR}/LICENSE"):
        assert (version.game_dir / name).is_file(), name
    assert read_model(version.smoke_report, SmokeReport).passed
    report = read_model(version.playtest_report, PlaytestReport)
    assert [run.bot for run in report.runs] == list(BotName)
    for bot in BotName:
        assert version.raw_video(bot).read_bytes()[:4] == WEBM_MAGIC
        assert read_telemetry(version.telemetry(bot))
        assert version.prepared_video(bot).stat().st_size > 1000
    assert version.console_log.is_file()
    assert version.combined_video.stat().st_size > 1000
    assert read_model(version.telemetry_summary, TelemetrySummary).bots
    read_model(version.critique_raw, Critique)
    read_model(version.critique_verified, VerifiedCritique)
    read_model(version.score, ScoreCard)
    assert version.changelog.is_file()
    assert cached_purposes(version)


@pytest.mark.browser
@pytest.mark.ffmpeg
async def test_a_real_run_leaves_the_hld_layout(browser: HarnessBrowser, tmp_path: Path) -> None:
    simple = fixture_game("simple")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(simple)],
            "critic": [hallucinating_critique()] * 2,
            "fixer": [fixed(simple, "I1", summary="Snappier jumps")],
        },
        real_stages(browser),
        budgets=Budgets(max_iterations=2),
    )

    summary = await loop.orchestrator.run()

    assert loop.run.config.is_file()
    assert loop.run.design_doc.read_text(encoding="utf-8") == DESIGN_DOC
    assert read_model(loop.run.summary, RunSummary) == summary
    assert summary.stop_reason is StopReason.MAX_ITERATIONS
    assert [record.version for record in summary.versions] == [1, 2]
    for record in summary.versions:
        version = loop.version(record.version)
        assert_hld_layout(version)
        assert record.score == read_model(version.score, ScoreCard).total
        assert record.outcome is not VersionOutcome.BUILD_FAILED
        verified = read_model(version.critique_verified, VerifiedCritique)
        statuses = {item.issue.id: item.status for item in verified.issues}
        assert statuses["I1"] is VerificationStatus.UNVERIFIABLE
        assert statuses["I2"] is VerificationStatus.CONTRADICTED
    assert cached_purposes(loop.version(1)) == ["builder", "critic"]
    assert cached_purposes(loop.version(2)) == ["critic", "fixer"]
    assert loop.version(1).changelog.read_text(encoding="utf-8") == ""
    assert loop.version(2).changelog.read_text(encoding="utf-8") == "- [I1] Snappier jumps\n"
    (request,) = loop.fixes
    assert "I2" not in issue_ids(request)


@pytest.mark.browser
@pytest.mark.ffmpeg
@pytest.mark.slow
async def test_a_real_regression_is_reverted(browser: HarnessBrowser, tmp_path: Path) -> None:
    simple = fixture_game("simple")
    unreachable_goal = fixture_game("simple", {"const GOAL = { x: 12,": "const GOAL = { x: 40,"})
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(simple)],
            "critic": [critique(3), critique(0), critique(3)],
            "fixer": [
                fixed(unreachable_goal, "I1", summary="Move the goal further away"),
                fixed(simple, "I1", summary="Put the goal back"),
            ],
        },
        real_stages(browser),
    )

    summary = await loop.orchestrator.run()

    first, second, third = summary.versions
    assert first.outcome is VersionOutcome.ACCEPTED
    assert second.outcome is VersionOutcome.REVERTED
    assert second.score < first.score
    assert third.parent_version == 1
    telemetry = read_model(loop.version(2).telemetry_summary, TelemetrySummary)
    assert not any(bot.completed for bot in telemetry.bots)
    request = loop.fixes[1]
    assert request.current == bundle(simple)
    assert request.rejected_attempt is not None
    assert request.rejected_attempt.attempt.version == 2
    assert read_game(loop.version(3).game_dir) == bundle(simple)


@pytest.mark.browser
@pytest.mark.ffmpeg
async def test_a_real_build_failure_is_repaired_by_a_quick_fix(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    simple = fixture_game("simple")
    loop = make_loop(
        tmp_path,
        {
            "builder": [built(fixture_game("no_api"))],
            "critic": [critique()],
            "fixer": [fixed(simple, "B1", summary="Add the window.iterplay harness API")],
        },
        real_stages(browser),
        budgets=Budgets(max_iterations=1),
    )

    summary = await loop.orchestrator.run()

    (quick_fix,) = loop.fixes
    assert quick_fix.current == bundle(fixture_game("no_api"))
    assert issue_ids(quick_fix) == SMOKE_ISSUE_IDS
    assert {item.origin for item in quick_fix.issues} == {IssueOrigin.SMOKE}
    assert "ready" in quick_fix.issues[0].issue.observation
    v1 = loop.version(1)
    smoke = read_model(v1.smoke_report, SmokeReport)
    assert (smoke.passed, smoke.attempt) == (True, 2)
    assert read_game(v1.game_dir) == bundle(simple)
    assert_hld_layout(v1)
    assert v1.changelog.read_text(encoding="utf-8") == (
        "- [B1] Add the window.iterplay harness API\n"
    )
    assert outcomes(summary) == [VersionOutcome.ACCEPTED]
