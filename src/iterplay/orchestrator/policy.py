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
        raise NotImplementedError

    @property
    def best_score(self) -> float | None:
        raise NotImplementedError

    def record(self, score: ScoreCard, *, booted: bool) -> KeepBestDecision:
        raise NotImplementedError


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
        """Why to stop now, or None to keep iterating. Called after every version."""
        raise NotImplementedError
