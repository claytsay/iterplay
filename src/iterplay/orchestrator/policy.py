"""Keep-best regression guard and stop conditions (HLD §4 [7])."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from iterplay.llm.metering import UsageMeter
from iterplay.models.run import Budgets, StopReason, VersionOutcome
from iterplay.models.scoring import ScoreCard


@dataclass(frozen=True, slots=True)
class KeepBestDecision:
    outcome: VersionOutcome

    base_version: int
    """Version the next Fixer call starts from."""


class KeepBestPolicy:
    """Tracks the best version so far and decides where the next fix starts.

    A booted version scoring at least the best so far is accepted and becomes the base; ties
    favour the newer version. A lower-scoring or unbootable version is reverted: the next fix
    starts from the best version, with the rejected attempt's issues attached as "what not to
    do". Until some version boots, the latest version is the base.
    """

    def __init__(self) -> None:
        self._best: tuple[int, float] | None = None

    @property
    def best_version(self) -> int | None:
        """The best booted version so far, the newest among equals; None until one boots."""
        return None if self._best is None else self._best[0]

    @property
    def best_score(self) -> float | None:
        """Total score of :attr:`best_version`."""
        return None if self._best is None else self._best[1]

    def record(self, score: ScoreCard, *, booted: bool) -> KeepBestDecision:
        """Decide the outcome of the newest version and where the next fix starts."""
        if booted and (self._best is None or score.total >= self._best[1]):
            self._best = (score.version, score.total)
            return KeepBestDecision(outcome=VersionOutcome.ACCEPTED, base_version=score.version)
        outcome = VersionOutcome.REVERTED if booted else VersionOutcome.BUILD_FAILED
        base_version = score.version if self._best is None else self._best[0]
        return KeepBestDecision(outcome=outcome, base_version=base_version)


class StopPolicy:
    """Ends the loop once the target is reached or any budget runs out."""

    def __init__(
        self,
        *,
        target: float,
        budgets: Budgets,
        meter: UsageMeter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = target
        self._budgets = budgets
        self._meter = meter
        self._clock = clock
        self._started_at = clock()

    def check(self, *, versions_built: int, best_score: float | None) -> StopReason | None:
        """Why to stop now, or None to keep iterating. Called after every version.

        When several reasons apply, the first of these wins: the target, the iteration limit,
        the wall-time budget (measured from construction), then the token and spend budgets.
        """
        if best_score is not None and best_score >= self._target:
            return StopReason.TARGET_REACHED
        if versions_built >= self._budgets.max_iterations:
            return StopReason.MAX_ITERATIONS
        max_wall_time_s = self._budgets.max_wall_time_s
        if max_wall_time_s is not None and self._clock() - self._started_at >= max_wall_time_s:
            return StopReason.TIME_BUDGET
        return self._meter.exhausted()
