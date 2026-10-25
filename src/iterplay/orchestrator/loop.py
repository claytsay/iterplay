"""The iteration loop (HLD §2): design doc, v1, playtest, critique, verify, score, fix, repeat."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from iterplay.agents.builder import Builder
from iterplay.agents.critic.base import CriticBackend
from iterplay.agents.fixer import Fixer
from iterplay.evaluation.scorer import Scorer
from iterplay.evaluation.verifier import Verifier
from iterplay.harness.playtest import PlaytestRunner
from iterplay.harness.smoke import SmokeTester
from iterplay.llm.metering import UsageMeter
from iterplay.media.prep import MediaPreparer
from iterplay.models.game import GameBundle
from iterplay.models.playtest import PlaytestReport
from iterplay.models.run import RunConfig, RunSummary
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.telemetry import TelemetrySummary
from iterplay.models.verification import VerifiedCritique
from iterplay.orchestrator.policy import KeepBestPolicy, StopPolicy
from iterplay.serving import LoopbackHTTPServer
from iterplay.storage.layout import RunPaths, VersionPaths

MAX_QUICK_RETRIES: Final = 2
"""Fixer retries after a failed smoke test before the version counts as BUILD_FAILED."""


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
    ) -> None:
        self._config = config
        self._run = run
        self._design_doc = design_doc
        self._agents = agents
        self._stages = stages
        self._keep_best = keep_best
        self._stop = stop
        self._meter = meter

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
        """
        raise NotImplementedError

    async def _boot(
        self, paths: VersionPaths, bundle: GameBundle, agents: Agents
    ) -> tuple[GameBundle, SmokeReport]:
        """Step 2: write and smoke-test a version, with quick Fixer retries.

        Returns the files that were last tested and their smoke report.
        """
        raise NotImplementedError

    async def _evaluate(
        self, paths: VersionPaths, smoke: SmokeReport, agents: Agents
    ) -> VersionEvaluation:
        """Step 3 for a version that booted."""
        raise NotImplementedError
