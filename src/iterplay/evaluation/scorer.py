"""[7] Scorer (HLD §4 [7])."""

from dataclasses import dataclass

from iterplay.models.playtest import PlaytestReport
from iterplay.models.rubric import Rubric
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.telemetry import TelemetrySummary
from iterplay.models.verification import VerifiedCritique


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    smoke: SmokeReport

    playtest: PlaytestReport | None
    """None when the build never booted; the playtest-based items then score 0."""

    telemetry: TelemetrySummary | None
    verified: VerifiedCritique | None

    headless: bool
    """Headless runs leave ``performance`` unmeasured (HLD §8)."""


class Scorer:
    """Weighted rubric score, mostly from objective telemetry.

    Objective items pass (1) or fail (0), with thresholds from each item's ``params``:

    - ``boots_cleanly``: every smoke check passed.
    - ``no_runtime_errors``: no uncaught errors in any run.
    - ``completable``: the Speedrunner completed the level.
    - ``collectibles_reachable``: every collectible was collected by at least one bot.
    - ``collision_integrity``: no unintentional ``oob`` events.
    - ``fair_difficulty``: the Newbie completed the level or died at most ``max_deaths`` times.
    - ``performance``: minimum FPS of at least ``min_fps``, in headed runs only.

    Critic items map the verified 0-5 assessment onto 0-1. The total is the weighted mean of
    the measured items.
    """

    def __init__(self, rubric: Rubric) -> None:
        self._rubric = rubric

    def score(self, version: int, inputs: ScoringInputs) -> ScoreCard:
        raise NotImplementedError
