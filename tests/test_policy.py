"""Keep-best regression guard and stop conditions (HLD §4 [7])."""

import pytest

from iterplay.llm.metering import UsageMeter
from iterplay.models.run import Budgets, StopReason, VersionOutcome
from iterplay.models.scoring import ScoreCard
from iterplay.orchestrator.policy import KeepBestDecision, KeepBestPolicy, StopPolicy

ACCEPTED = VersionOutcome.ACCEPTED
REVERTED = VersionOutcome.REVERTED
BUILD_FAILED = VersionOutcome.BUILD_FAILED


def card(version: int, total: float) -> ScoreCard:
    return ScoreCard(version=version, items=[], total=total, target=0.9, meets_target=total >= 0.9)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class StubMeter(UsageMeter):
    """A meter whose token or spend budget runs out on command."""

    def __init__(self, exhausted: StopReason | None = None) -> None:
        super().__init__(Budgets(), None)
        self.reason = exhausted

    def exhausted(self) -> StopReason | None:
        return self.reason


def test_nothing_is_best_before_a_version_boots() -> None:
    policy = KeepBestPolicy()

    assert (policy.best_version, policy.best_score) == (None, None)


def test_the_first_version_to_boot_is_accepted_whatever_its_score() -> None:
    policy = KeepBestPolicy()

    decision = policy.record(card(1, 0.1), booted=True)

    assert decision == KeepBestDecision(outcome=ACCEPTED, base_version=1)
    assert (policy.best_version, policy.best_score) == (1, 0.1)


def test_a_better_version_is_accepted_and_becomes_the_base() -> None:
    policy = KeepBestPolicy()
    policy.record(card(1, 0.4), booted=True)

    assert policy.record(card(2, 0.6), booted=True) == KeepBestDecision(ACCEPTED, 2)
    assert (policy.best_version, policy.best_score) == (2, 0.6)


def test_a_tie_favours_the_newer_version() -> None:
    policy = KeepBestPolicy()
    policy.record(card(1, 0.5), booted=True)

    assert policy.record(card(2, 0.5), booted=True) == KeepBestDecision(ACCEPTED, 2)
    assert policy.best_version == 2


def test_a_regression_is_reverted_to_the_best_version() -> None:
    policy = KeepBestPolicy()
    policy.record(card(1, 0.4), booted=True)
    policy.record(card(2, 0.7), booted=True)

    assert policy.record(card(3, 0.5), booted=True) == KeepBestDecision(REVERTED, 2)
    assert (policy.best_version, policy.best_score) == (2, 0.7)


def test_a_build_failure_restarts_from_the_best_version() -> None:
    policy = KeepBestPolicy()
    policy.record(card(1, 0.4), booted=True)

    assert policy.record(card(2, 0.0), booted=False) == KeepBestDecision(BUILD_FAILED, 1)
    assert (policy.best_version, policy.best_score) == (1, 0.4)


def test_an_unbooted_version_never_becomes_the_best_whatever_its_score() -> None:
    policy = KeepBestPolicy()
    policy.record(card(1, 0.3), booted=True)

    assert policy.record(card(2, 0.95), booted=False) == KeepBestDecision(BUILD_FAILED, 1)
    assert policy.best_score == 0.3


def test_until_a_version_boots_the_latest_version_is_the_base() -> None:
    policy = KeepBestPolicy()

    assert policy.record(card(1, 0.0), booted=False) == KeepBestDecision(BUILD_FAILED, 1)
    assert policy.record(card(2, 0.0), booted=False) == KeepBestDecision(BUILD_FAILED, 2)
    assert policy.best_version is None
    assert policy.record(card(3, 0.2), booted=True) == KeepBestDecision(ACCEPTED, 3)


def test_a_whole_run_keeps_the_best_version_as_the_base() -> None:
    policy = KeepBestPolicy()
    story = [
        (1, 0.35, True, ACCEPTED, 1),
        (2, 0.55, True, ACCEPTED, 2),
        (3, 0.40, True, REVERTED, 2),
        (4, 0.00, False, BUILD_FAILED, 2),
        (5, 0.55, True, ACCEPTED, 5),
        (6, 0.92, True, ACCEPTED, 6),
    ]

    decisions = [
        (policy.record(card(version, total), booted=booted).outcome, policy.best_version)
        for version, total, booted, _, _ in story
    ]

    assert decisions == [(outcome, best) for _, _, _, outcome, best in story]


def test_the_loop_continues_while_nothing_has_run_out() -> None:
    policy = StopPolicy(target=0.9, budgets=Budgets(), meter=StubMeter(), clock=FakeClock())

    assert policy.check(versions_built=1, best_score=None) is None
    assert policy.check(versions_built=4, best_score=0.89) is None


@pytest.mark.parametrize(
    ("best_score", "reason"),
    [(0.9, StopReason.TARGET_REACHED), (0.95, StopReason.TARGET_REACHED), (0.8999, None)],
)
def test_the_loop_stops_once_the_best_score_reaches_the_target(
    best_score: float, reason: StopReason | None
) -> None:
    policy = StopPolicy(target=0.9, budgets=Budgets(), meter=StubMeter(), clock=FakeClock())

    assert policy.check(versions_built=2, best_score=best_score) is reason


@pytest.mark.parametrize(
    ("versions_built", "reason"),
    [(2, None), (3, StopReason.MAX_ITERATIONS), (4, StopReason.MAX_ITERATIONS)],
)
def test_the_loop_stops_after_max_iterations_versions(
    versions_built: int, reason: StopReason | None
) -> None:
    budgets = Budgets(max_iterations=3)
    policy = StopPolicy(target=0.9, budgets=budgets, meter=StubMeter(), clock=FakeClock())

    assert policy.check(versions_built=versions_built, best_score=0.5) is reason


def test_the_wall_time_budget_counts_from_when_the_policy_was_created() -> None:
    clock = FakeClock(now=500.0)
    budgets = Budgets(max_wall_time_s=60)
    policy = StopPolicy(target=0.9, budgets=budgets, meter=StubMeter(), clock=clock)

    clock.now = 559.9
    assert policy.check(versions_built=1, best_score=0.5) is None
    clock.now = 560.0
    assert policy.check(versions_built=1, best_score=0.5) is StopReason.TIME_BUDGET


def test_without_a_wall_time_budget_time_never_runs_out() -> None:
    clock = FakeClock()
    policy = StopPolicy(target=0.9, budgets=Budgets(), meter=StubMeter(), clock=clock)

    clock.now += 1e9

    assert policy.check(versions_built=1, best_score=0.5) is None


@pytest.mark.parametrize("reason", [StopReason.TOKEN_BUDGET, StopReason.SPEND_BUDGET])
def test_the_loop_stops_once_the_meter_runs_out(reason: StopReason) -> None:
    policy = StopPolicy(target=0.9, budgets=Budgets(), meter=StubMeter(reason), clock=FakeClock())

    assert policy.check(versions_built=1, best_score=0.5) is reason


@pytest.mark.parametrize(
    ("best_score", "versions_built", "elapsed_s", "metered", "reason"),
    [
        (0.95, 5, 120.0, StopReason.TOKEN_BUDGET, StopReason.TARGET_REACHED),
        (0.50, 5, 120.0, StopReason.TOKEN_BUDGET, StopReason.MAX_ITERATIONS),
        (0.50, 2, 120.0, StopReason.SPEND_BUDGET, StopReason.TIME_BUDGET),
        (0.50, 2, 30.0, StopReason.SPEND_BUDGET, StopReason.SPEND_BUDGET),
        (None, 2, 30.0, None, None),
    ],
)
def test_stop_reasons_take_precedence_in_a_fixed_order(
    best_score: float | None,
    versions_built: int,
    elapsed_s: float,
    metered: StopReason | None,
    reason: StopReason | None,
) -> None:
    clock = FakeClock(now=0.0)
    budgets = Budgets(max_iterations=5, max_wall_time_s=60)
    policy = StopPolicy(target=0.9, budgets=budgets, meter=StubMeter(metered), clock=clock)
    clock.now = elapsed_s

    assert policy.check(versions_built=versions_built, best_score=best_score) is reason


def test_the_real_clock_is_used_by_default() -> None:
    policy = StopPolicy(target=0.9, budgets=Budgets(max_wall_time_s=3600), meter=StubMeter())

    assert policy.check(versions_built=1, best_score=0.5) is None
