"""The iteration loop (HLD §2): design doc, v1, playtest, critique, verify, score, fix, repeat."""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeAlias

from iterplay.agents.builder import Builder
from iterplay.agents.critic.base import CriticBackend, CriticInput
from iterplay.agents.fixer import AttemptSummary, Fixer, FixRequest, FixResult, RejectedAttempt
from iterplay.errors import BudgetExceededError, GuardrailError, LLMError, MediaError
from iterplay.evaluation.issues import fixer_queue, smoke_failure_issues
from iterplay.evaluation.scorer import Scorer, ScoringInputs
from iterplay.evaluation.telemetry_summary import summarize
from iterplay.evaluation.verifier import Evidence, Verifier
from iterplay.harness.playtest import PlaytestRunner
from iterplay.harness.smoke import SmokeTester
from iterplay.llm.metering import UsageMeter
from iterplay.media.prep import MediaPreparer
from iterplay.models.critique import Critique
from iterplay.models.game import ChangelogEntry, GameBundle
from iterplay.models.playtest import BotName, PlaytestReport
from iterplay.models.run import RunConfig, RunSummary, StopReason, TokenUsage, VersionRecord
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.telemetry import TelemetryRecord, TelemetrySummary
from iterplay.models.verification import VerifiedCritique, VerifiedIssue
from iterplay.orchestrator.policy import KeepBestDecision, KeepBestPolicy, StopPolicy
from iterplay.serving import LoopbackHTTPServer
from iterplay.storage.artifacts import write_model, write_text
from iterplay.storage.game_files import read_game, write_game
from iterplay.storage.layout import RunPaths, VersionPaths
from iterplay.storage.telemetry import read_telemetry

MAX_QUICK_RETRIES: Final = 2
"""Fixer retries after a failed smoke test before the version counts as BUILD_FAILED."""

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Agents:
    builder: Builder
    critic: CriticBackend
    fixer: Fixer


AgentFactory: TypeAlias = Callable[[VersionPaths], Agents]
"""Builds the model-backed agents for one version, caching their calls in its ``llm/``."""

GameServerFactory: TypeAlias = Callable[[Path], LoopbackHTTPServer]
"""Creates a 127.0.0.1 server for a version's ``game/`` directory."""


@dataclass(frozen=True, slots=True)
class Stages:
    """The local, deterministic stages of one iteration."""

    smoke: SmokeTester
    playtest: PlaytestRunner
    media: MediaPreparer
    verifier: Verifier
    scorer: Scorer
    serve_game: GameServerFactory


@dataclass(frozen=True, slots=True)
class VersionEvaluation:
    """Everything learned about one version; drives keep-best and the next fix."""

    version: int
    smoke: SmokeReport

    playtest: PlaytestReport | None
    """None, like the fields below, when the build never booted."""

    telemetry: TelemetrySummary | None
    verified: VerifiedCritique | None
    score: ScoreCard


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A version's files before they are booted, with where they came from."""

    version: int
    parent_version: int | None

    bundle: GameBundle
    changes: tuple[ChangelogEntry, ...]
    """Patch notes of the Fixer call that produced ``bundle``; empty for v1."""

    agents: Agents
    """The version's own agents, which cache their calls in its ``llm/``."""

    started_at: float
    usage_before: TokenUsage
    """Run-wide token usage just before the version's first model call."""


@dataclass(slots=True)
class _Progress:
    """The versions a run has recorded so far."""

    started_at: float
    records: list[VersionRecord] = field(default_factory=list)
    attempts: list[AttemptSummary] = field(default_factory=list)
    evaluations: dict[int, VersionEvaluation] = field(default_factory=dict)

    @property
    def latest(self) -> VersionEvaluation:
        return self.evaluations[self.records[-1].version]

    def add(
        self,
        record: VersionRecord,
        evaluation: VersionEvaluation,
        changes: tuple[ChangelogEntry, ...],
    ) -> None:
        """Record a finished version, and what it tried for the Fixer's history."""
        parent = None if record.parent_version is None else self.evaluations[record.parent_version]
        self.records.append(record)
        self.evaluations[record.version] = evaluation
        self.attempts.append(
            AttemptSummary(
                version=record.version,
                parent_version=record.parent_version,
                score=record.score,
                score_delta=None if parent is None else record.score - parent.score.total,
                outcome=record.outcome,
                changes=changes,
            )
        )


class Orchestrator:
    """Drives one run from the design doc to its final version, persisting every artifact."""

    def __init__(
        self,
        *,
        config: RunConfig,
        run: RunPaths,
        design_doc: str,
        agents: AgentFactory,
        stages: Stages,
        keep_best: KeepBestPolicy,
        stop: StopPolicy,
        meter: UsageMeter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``clock`` times each version and the run; tests replace :func:`time.monotonic`."""
        self._config = config
        self._run = run
        self._design_doc = design_doc
        self._agents = agents
        self._stages = stages
        self._keep_best = keep_best
        self._stop = stop
        self._meter = meter
        self._clock = clock
        self._started = False

    async def run(self) -> RunSummary:
        """Iterate until the stop policy fires, then return the final ``summary.json``.

        1. Build v1 from the design doc.
        2. Boot: write the files and smoke-test them. On failure, hand the errors to the
           Fixer as build-failure issues, up to ``MAX_QUICK_RETRIES`` times.
        3. Evaluate: playtest, media prep, critic, Verifier and Scorer, persisting each
           artifact as it is produced.
        4. Keep-best: record the score. After a regression, the next fix starts from the best
           version, with the regressed attempt attached as "what not to do".
        5. Rewrite ``summary.json`` so a crash still leaves a replayable run, then consult
           the stop policy.
        6. Fix: the Fixer turns the base version, ranked issues and attempt history into the
           next version; continue from step 2.

        Each version's agents are made for its own directory, so every model call that
        produces or judges a version is cached in that version's ``llm/``, and its duration
        and token usage run from that first call to its score. A version's ``changelog.md``
        lists the Fixer's patch notes, including those of its quick fixes.

        Failures degrade where the loop can carry on. A quick fix that fails (``LLMError`` or
        ``GuardrailError``) ends the retries, so the version counts as ``BUILD_FAILED``. A
        critic that fails, or has no clip to watch, leaves the critique empty: the version is
        still verified and scored, and its critic items stay unmeasured. A
        ``BudgetExceededError`` from any model call stops the run with the matching
        ``StopReason``; the version in progress is not recorded, and ``summary.json`` is
        still written. Any other error propagates, and ``summary.json`` keeps the versions
        recorded before it.

        Raises:
            RuntimeError: This orchestrator has already run; each one drives a single run.
        """
        if self._started:
            raise RuntimeError("This Orchestrator has already run; create a new one per run")
        self._started = True
        progress = _Progress(started_at=self._clock())
        try:
            reason = await self._iterate(progress)
        except BudgetExceededError as exc:
            reason = self._meter.exhausted() or StopReason.TOKEN_BUDGET
            _log.warning("Stopping %s early: %s", self._config.run_id, exc)
        summary = self._summary(progress, reason)
        write_model(self._run.summary, summary)
        _log.info(
            "Run %s stopped: %s. Versions recorded: %d; best: %s",
            self._config.run_id,
            reason.value,
            len(summary.versions),
            "none" if summary.best_version is None else f"v{summary.best_version}",
        )
        return summary

    async def _iterate(self, progress: _Progress) -> StopReason:
        """Steps 1-6, until the stop policy gives a reason."""
        candidate = await self._build()
        while True:
            decision = await self._complete(candidate, progress)
            reason = self._stop.check(
                versions_built=len(progress.records), best_score=self._keep_best.best_score
            )
            if reason is not None:
                return reason
            candidate = await self._fix(progress, decision)

    async def _build(self) -> _Candidate:
        """Step 1: v1 from the design doc."""
        started_at, usage_before = self._clock(), self._meter.total
        agents = self._agents(self._run.version(1))
        _log.info("v1: building the game from the design doc")
        built = await agents.builder.build(self._design_doc)
        return _Candidate(
            version=1,
            parent_version=None,
            bundle=built.bundle,
            changes=(),
            agents=agents,
            started_at=started_at,
            usage_before=usage_before,
        )

    async def _complete(self, candidate: _Candidate, progress: _Progress) -> KeepBestDecision:
        """Steps 2 to 5 for one version: boot, evaluate, keep-best, then record it."""
        version = candidate.version
        smoke, quick_changes = await self._boot(
            version, candidate.bundle, candidate.agents, progress.attempts
        )
        changes = (*candidate.changes, *quick_changes)
        write_text(self._run.version(version).changelog, _changelog(changes))
        if smoke.passed:
            evaluation = await self._evaluate(version, smoke, candidate.agents)
        else:
            evaluation = self._unbooted(version, smoke)
        decision = self._keep_best.record(evaluation.score, booted=smoke.passed)
        record = VersionRecord(
            version=version,
            parent_version=candidate.parent_version,
            score=evaluation.score.total,
            outcome=decision.outcome,
            duration_s=max(0.0, self._clock() - candidate.started_at),
            token_usage=_usage_since(candidate.usage_before, self._meter.total),
        )
        progress.add(record, evaluation, changes)
        write_model(self._run.summary, self._summary(progress, stop_reason=None))
        _log.info(
            "v%d scored %.1f%% (%s)",
            version,
            record.score * 100,
            record.outcome.value.replace("_", " "),
        )
        return decision

    async def _boot(
        self,
        version: int,
        bundle: GameBundle,
        agents: Agents,
        history: Sequence[AttemptSummary],
    ) -> tuple[SmokeReport, tuple[ChangelogEntry, ...]]:
        """Step 2: write and smoke-test a version, with quick Fixer retries.

        Every attempt rewrites ``game/`` and ``smoke.json``, so both end up holding the last
        files tested. Returns the last smoke report and the quick fixes' patch notes.
        """
        changes: list[ChangelogEntry] = []
        attempt = 1
        smoke = await self._smoke_test(version, bundle, attempt=attempt)
        while not smoke.passed and attempt <= MAX_QUICK_RETRIES:
            fixed = await self._quick_fix(version, bundle, smoke, agents, history)
            if fixed is None:
                break
            bundle = fixed.files.bundle
            changes.extend(fixed.changelog)
            attempt += 1
            smoke = await self._smoke_test(version, bundle, attempt=attempt)
        return smoke, tuple(changes)

    async def _smoke_test(self, version: int, bundle: GameBundle, *, attempt: int) -> SmokeReport:
        """Write the files, serve them on 127.0.0.1 and smoke-test them."""
        paths = self._run.version(version)
        write_game(paths.game_dir, bundle)
        with self._stages.serve_game(paths.game_dir) as server:
            smoke = await self._stages.smoke.run(server.base_url, attempt=attempt)
        write_model(paths.smoke_report, smoke)
        if smoke.passed:
            _log.info("v%d passed its smoke test on attempt %d", version, attempt)
        return smoke

    async def _quick_fix(
        self,
        version: int,
        bundle: GameBundle,
        smoke: SmokeReport,
        agents: Agents,
        history: Sequence[AttemptSummary],
    ) -> FixResult | None:
        """Ask the Fixer to repair a build that failed its smoke test; None if it fails."""
        failed = ", ".join(result.check.value for result in smoke.checks if not result.passed)
        _log.warning(
            "v%d failed its smoke test on attempt %d (%s); asking the Fixer for a quick fix",
            version,
            smoke.attempt,
            failed or "no check named",
        )
        request = FixRequest(
            design_doc=self._design_doc,
            current=bundle,
            issues=tuple(smoke_failure_issues(smoke)),
            history=tuple(history),
        )
        try:
            return await agents.fixer.fix(request)
        except (LLMError, GuardrailError) as exc:
            _log.warning(
                "v%d: the quick fix failed, so the build counts as failed: %s", version, exc
            )
            return None

    async def _evaluate(
        self, version: int, smoke: SmokeReport, agents: Agents
    ) -> VersionEvaluation:
        """Step 3 for a version that booted."""
        paths = self._run.version(version)
        playtest = await self._playtest(version)
        telemetry = {run.bot: _read_bot_telemetry(paths, run.bot) for run in playtest.runs}
        summary = summarize(telemetry)
        write_model(paths.telemetry_summary, summary)
        critique = await self._critique(version, playtest, summary, agents)
        verified = self._stages.verifier.verify(
            critique, Evidence(playtest=playtest, telemetry=telemetry, summary=summary)
        )
        write_model(paths.critique_verified, verified)
        inputs = ScoringInputs(
            smoke=smoke,
            playtest=playtest,
            telemetry=summary,
            verified=verified,
            headless=self._config.headless,
        )
        return VersionEvaluation(
            version=version,
            smoke=smoke,
            playtest=playtest,
            telemetry=summary,
            verified=verified,
            score=self._score(version, inputs),
        )

    def _unbooted(self, version: int, smoke: SmokeReport) -> VersionEvaluation:
        """Step 3 for a version that never booted: it scores 0 on every item."""
        inputs = ScoringInputs(
            smoke=smoke,
            playtest=None,
            telemetry=None,
            verified=None,
            headless=self._config.headless,
        )
        return VersionEvaluation(
            version=version,
            smoke=smoke,
            playtest=None,
            telemetry=None,
            verified=None,
            score=self._score(version, inputs),
        )

    async def _playtest(self, version: int) -> PlaytestReport:
        paths = self._run.version(version)
        _log.info("v%d: playtesting", version)
        with self._stages.serve_game(paths.game_dir) as server:
            return await self._stages.playtest.run(
                server.base_url,
                game_seed=self._config.game_seed,
                bot_seeds=self._config.bot_seeds,
                paths=paths,
            )

    async def _critique(
        self, version: int, playtest: PlaytestReport, summary: TelemetrySummary, agents: Agents
    ) -> Critique:
        """The critic's findings, persisted as ``critique.raw.json``.

        Empty, and not persisted, when no clip could be prepared or the critic failed.
        """
        paths = self._run.version(version)
        try:
            media = await self._stages.media.prepare(paths, _clip_order(playtest))
        except MediaError as exc:
            _log.warning("v%d has no clip to critique, so only telemetry counts: %s", version, exc)
            return Critique(issues=[])
        request = CriticInput(
            design_doc=self._design_doc,
            rubric=self._config.rubric,
            media=media,
            telemetry=summary,
            console_errors=_console_errors(playtest),
        )
        _log.info("v%d: asking the critic", version)
        try:
            critique = await agents.critic.critique(request)
        except LLMError as exc:
            _log.warning("v%d: the critic failed, so only telemetry counts: %s", version, exc)
            return Critique(issues=[])
        write_model(paths.critique_raw, critique)
        return critique

    def _score(self, version: int, inputs: ScoringInputs) -> ScoreCard:
        score = self._stages.scorer.score(version, inputs)
        write_model(self._run.version(version).score, score)
        return score

    async def _fix(self, progress: _Progress, decision: KeepBestDecision) -> _Candidate:
        """Step 6: the next version, from the Fixer, with its own agents."""
        version = progress.records[-1].version + 1
        started_at, usage_before = self._clock(), self._meter.total
        agents = self._agents(self._run.version(version))
        request = self._fix_request(progress, decision)
        _log.info(
            "v%d: fixing v%d, with %d issues to address",
            version,
            decision.base_version,
            len(request.issues),
        )
        fixed = await agents.fixer.fix(request)
        return _Candidate(
            version=version,
            parent_version=decision.base_version,
            bundle=fixed.files.bundle,
            changes=fixed.changelog,
            agents=agents,
            started_at=started_at,
            usage_before=usage_before,
        )

    def _fix_request(self, progress: _Progress, decision: KeepBestDecision) -> FixRequest:
        """The base version's files and work queue, the attempt history, and the latest
        attempt as "what not to do" when the fix starts from another version."""
        latest = progress.latest
        base = progress.evaluations[decision.base_version]
        rejected = None
        if base.version != latest.version:
            rejected = RejectedAttempt(
                attempt=progress.attempts[-1], issues=tuple(_work_queue(latest))
            )
        return FixRequest(
            design_doc=self._design_doc,
            current=read_game(self._run.version(base.version).game_dir),
            issues=tuple(_work_queue(base)),
            history=tuple(progress.attempts),
            rejected_attempt=rejected,
        )

    def _summary(self, progress: _Progress, stop_reason: StopReason | None) -> RunSummary:
        return RunSummary(
            run_id=self._config.run_id,
            versions=list(progress.records),
            best_version=self._keep_best.best_version,
            stop_reason=stop_reason,
            duration_s=max(0.0, self._clock() - progress.started_at),
            token_usage=self._meter.total,
            spend_usd=self._meter.spend_usd,
        )


def _work_queue(evaluation: VersionEvaluation) -> list[VerifiedIssue]:
    """A version's ranked issues for the Fixer: its smoke failures if it never booted."""
    if evaluation.verified is None:
        return smoke_failure_issues(evaluation.smoke)
    return fixer_queue(evaluation.verified)


def _changelog(changes: Sequence[ChangelogEntry]) -> str:
    """Patch notes as Markdown bullets, e.g. ``- [I3, D1] Register the wall colliders``."""
    return "".join(f"- {_change(entry)}\n" for entry in changes)


def _change(entry: ChangelogEntry) -> str:
    issue_ids = ", ".join(issue_id.strip() for issue_id in entry.issue_ids)
    return f"[{issue_ids}] {' '.join(entry.summary.split())}".rstrip()


def _usage_since(before: TokenUsage, now: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=max(0, now.input_tokens - before.input_tokens),
        output_tokens=max(0, now.output_tokens - before.output_tokens),
    )


def _read_bot_telemetry(paths: VersionPaths, bot: BotName) -> list[TelemetryRecord]:
    """One bot's snapshots; none if its session left no telemetry file."""
    path = paths.telemetry(bot)
    try:
        return read_telemetry(path)
    except FileNotFoundError:
        _log.warning("The %s run left no telemetry at %s", bot, path)
        return []


def _clip_order(playtest: PlaytestReport) -> list[BotName]:
    """The bots that played, in the order their clips appear in the combined video."""
    played = {run.bot for run in playtest.runs}
    return [bot for bot in BotName if bot in played]


def _console_errors(playtest: PlaytestReport) -> tuple[str, ...]:
    """Every run's uncaught and console errors, in run order."""
    return tuple(
        error for run in playtest.runs for error in (*run.uncaught_errors, *run.console_errors)
    )
