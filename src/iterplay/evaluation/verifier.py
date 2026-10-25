"""[6] Verifier: checks every critic claim against telemetry (HLD §4 [6])."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from iterplay.contract.state import KILL_PLANE_Y
from iterplay.models.critique import Critique, Issue
from iterplay.models.playtest import BotName, PlaytestReport
from iterplay.models.telemetry import TelemetryRecord, TelemetrySummary
from iterplay.models.verification import VerificationStatus, VerifiedCritique, VerifiedIssue


@dataclass(frozen=True, slots=True)
class VerifierConfig:
    time_tolerance_s: float = 2.0
    """How far from a claim's timestamp corroborating telemetry may be."""

    kill_plane_y: float = KILL_PLANE_Y

    min_fps: float = 30.0
    """A stutter claim needs the FPS to drop below this near its timestamp."""

    stuck_issue_s: float = 5.0
    """Stuck intervals at least this long become deterministic issues."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Locally produced ground truth that claims are checked against."""

    playtest: PlaytestReport
    telemetry: Mapping[BotName, Sequence[TelemetryRecord]]
    summary: TelemetrySummary


class Verifier:
    """Deterministic, local check of each critic claim before the Fixer sees it.

    Claims are routed by rubric item:

    - ``collision_integrity``: confirmed by an ``oob`` event or ``y < kill_plane_y`` for that
      bot within ``time_tolerance_s`` of the timestamp, otherwise contradicted.
    - ``completable``: a "level impossible" claim is contradicted if any bot completed it.
    - ``collectibles_reachable``: confirmed if some collectible was never collected,
      contradicted if every collectible was.
    - ``fair_difficulty``: confirmed by deaths near the timestamp, otherwise unverifiable.
    - ``performance``: confirmed by an FPS dip below ``min_fps`` near the timestamp.
    - ``no_runtime_errors``: confirmed by a matching uncaught or console error.
    - anything else (design-doc match, feel): unverifiable, kept at lower priority.

    Contradicted claims are kept in the output, so the viewer can show them as caught
    hallucinations, but never reach the Fixer. The Verifier also adds deterministic issues
    the critic missed: uncaught JS errors, collectibles no bot collected, and bots stuck for
    ``stuck_issue_s`` or longer.
    """

    def __init__(self, config: VerifierConfig | None = None) -> None:
        self._config = config or VerifierConfig()

    def verify(self, critique: Critique, evidence: Evidence) -> VerifiedCritique:
        raise NotImplementedError

    def _check(self, issue: Issue, evidence: Evidence) -> tuple[VerificationStatus, str]:
        """Status and rationale for one critic claim."""
        raise NotImplementedError

    def _deterministic_issues(self, evidence: Evidence) -> list[VerifiedIssue]:
        raise NotImplementedError
