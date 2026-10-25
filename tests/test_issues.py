"""Evaluation results become the Fixer's ranked work queue (HLD §4 [2], [8])."""

from collections.abc import Sequence

import pytest

from iterplay.evaluation.issues import DEFAULT_QUEUE_LIMIT, fixer_queue, smoke_failure_issues
from iterplay.models.critique import EvidenceType, Issue, Severity
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)

CONFIRMED = VerificationStatus.CONFIRMED
UNVERIFIABLE = VerificationStatus.UNVERIFIABLE
CONTRADICTED = VerificationStatus.CONTRADICTED


def smoke(
    *failed: SmokeCheck,
    details: dict[SmokeCheck, str] | None = None,
    uncaught: Sequence[str] = (),
    console: Sequence[str] = (),
    attempt: int = 1,
) -> SmokeReport:
    """A report in which every check passed except ``failed``."""
    details = details or {}
    checks = [
        SmokeCheckResult(check=check, passed=check not in failed, detail=details.get(check, ""))
        for check in SmokeCheck
    ]
    return SmokeReport(
        passed=not failed,
        checks=checks,
        uncaught_errors=list(uncaught),
        console_errors=list(console),
        attempt=attempt,
    )


def verified_issue(
    issue_id: str,
    severity: Severity = Severity.MEDIUM,
    status: VerificationStatus = CONFIRMED,
    origin: IssueOrigin = IssueOrigin.CRITIC,
) -> VerifiedIssue:
    issue = Issue(
        id=issue_id,
        rubric_item="completable",
        severity=severity,
        observation="Something is wrong.",
        evidence_type=EvidenceType.TELEMETRY,
    )
    return VerifiedIssue(issue=issue, origin=origin, status=status, rationale="Because.")


def queue_ids(issues: Sequence[VerifiedIssue], limit: int | None = None) -> list[str]:
    return [issue.issue.id for issue in fixer_queue(VerifiedCritique(issues=list(issues)), limit)]


def test_a_passing_smoke_test_has_no_issues() -> None:
    assert smoke_failure_issues(smoke()) == []


def test_each_failed_check_becomes_a_confirmed_critical_boot_issue() -> None:
    report = smoke(SmokeCheck.READY, SmokeCheck.PLAYER_MOVES, attempt=2)

    issues = smoke_failure_issues(report)

    assert [issue.issue.id for issue in issues] == ["B1", "B2"]
    assert [issue.issue.observation.split(" failed:")[0] for issue in issues] == [
        "Smoke check ready",
        "Smoke check player_moves",
    ]
    for issue in issues:
        assert (issue.origin, issue.status) == (IssueOrigin.SMOKE, CONFIRMED)
        assert issue.issue.rubric_item == "boots_cleanly"
        assert issue.issue.severity is Severity.CRITICAL
        assert issue.issue.evidence_type is EvidenceType.CONSOLE
        assert (issue.issue.bot, issue.issue.timestamp) == (None, None)
        assert issue.issue.fix_hint
        assert issue.rationale == "Reported by the smoke test on attempt 2."


def test_a_failed_check_is_described_by_its_detail() -> None:
    report = smoke(SmokeCheck.READY, details={SmokeCheck.READY: "not ready after 10 s"})

    [issue] = smoke_failure_issues(report)

    assert issue.issue.observation == "Smoke check ready failed: not ready after 10 s."


@pytest.mark.parametrize("check", list(SmokeCheck))
def test_a_failed_check_without_detail_is_explained(check: SmokeCheck) -> None:
    [issue] = smoke_failure_issues(smoke(check))

    prefix = f"Smoke check {check.value} failed: "
    assert issue.issue.observation.startswith(prefix)
    assert len(issue.issue.observation) > len(prefix) + 10


def test_the_first_few_distinct_errors_are_quoted_uncaught_first() -> None:
    report = smoke(
        SmokeCheck.NO_ERRORS,
        details={SmokeCheck.NO_ERRORS: "2 uncaught errors"},
        uncaught=["TypeError: a\n    at main.js:3", "RangeError: b"],
        console=["TypeError: a\n    at main.js:3", "404 favicon.ico", "WebGL lost"],
    )

    [issue] = smoke_failure_issues(report)

    assert issue.issue.observation == (
        "Smoke check no_errors failed: 2 uncaught errors. Errors: TypeError: a at main.js:3; "
        "RangeError: b; 404 favicon.ico (and 1 more)."
    )


def test_long_errors_are_shortened() -> None:
    report = smoke(SmokeCheck.NO_ERRORS, uncaught=["E" * 2000])

    [issue] = smoke_failure_issues(report)

    assert len(issue.issue.observation) < 600
    assert issue.issue.observation.endswith("EEE....")


def test_every_issue_of_a_failed_build_carries_its_errors() -> None:
    report = smoke(SmokeCheck.READY, SmokeCheck.STATE_SCHEMA, uncaught=["SyntaxError: x"])

    issues = smoke_failure_issues(report)

    assert all(issue.issue.observation.endswith("Errors: SyntaxError: x.") for issue in issues)


def test_a_failed_report_without_a_failed_check_still_yields_an_issue() -> None:
    report = SmokeReport(passed=False, checks=[], uncaught_errors=["Page crashed"], attempt=3)

    [issue] = smoke_failure_issues(report)

    assert issue.issue.id == "B1"
    assert issue.issue.observation == "The smoke test failed. Errors: Page crashed."


def test_queue_ranks_confirmed_issues_by_severity() -> None:
    issues = [
        verified_issue("low", Severity.LOW),
        verified_issue("critical", Severity.CRITICAL),
        verified_issue("medium", Severity.MEDIUM),
        verified_issue("high", Severity.HIGH),
    ]

    assert queue_ids(issues) == ["critical", "high", "medium", "low"]


def test_queue_keeps_the_order_of_equally_severe_issues() -> None:
    issues = [
        verified_issue("I1", Severity.HIGH),
        verified_issue("D1", Severity.CRITICAL, origin=IssueOrigin.DETERMINISTIC),
        verified_issue("I2", Severity.HIGH),
        verified_issue("I3", Severity.CRITICAL),
        verified_issue("D2", Severity.HIGH, origin=IssueOrigin.DETERMINISTIC),
    ]

    assert queue_ids(issues) == ["D1", "I3", "I1", "I2", "D2"]


def test_unverifiable_issues_follow_every_confirmed_one() -> None:
    issues = [
        verified_issue("U-critical", Severity.CRITICAL, UNVERIFIABLE),
        verified_issue("C-low", Severity.LOW),
        verified_issue("U-high", Severity.HIGH, UNVERIFIABLE),
        verified_issue("C-medium", Severity.MEDIUM),
    ]

    assert queue_ids(issues) == ["C-medium", "C-low", "U-critical", "U-high"]


def test_contradicted_issues_never_reach_the_fixer() -> None:
    issues = [
        verified_issue("hallucination", Severity.CRITICAL, CONTRADICTED),
        verified_issue("real", Severity.LOW),
    ]

    assert queue_ids(issues) == ["real"]


def test_queue_is_capped_at_the_default_limit() -> None:
    issues = [verified_issue(f"I{n}") for n in range(DEFAULT_QUEUE_LIMIT + 3)]

    queue = fixer_queue(VerifiedCritique(issues=issues))

    assert DEFAULT_QUEUE_LIMIT == 5
    assert [issue.issue.id for issue in queue] == [f"I{n}" for n in range(DEFAULT_QUEUE_LIMIT)]


@pytest.mark.parametrize(("limit", "expected"), [(None, 8), (0, 0), (2, 2), (20, 8)])
def test_queue_limit_can_be_changed_or_lifted(limit: int | None, expected: int) -> None:
    issues = [verified_issue(f"I{n}") for n in range(8)]

    assert len(queue_ids(issues, limit)) == expected


def test_a_negative_queue_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 0"):
        fixer_queue(VerifiedCritique(issues=[]), -1)


def test_smoke_issues_queue_like_any_other_confirmed_issue() -> None:
    issues = smoke_failure_issues(smoke(SmokeCheck.READY, SmokeCheck.NO_ERRORS))
    critique = VerifiedCritique(issues=[verified_issue("I1", Severity.HIGH), *issues])

    assert [issue.issue.id for issue in fixer_queue(critique)] == ["B1", "B2", "I1"]
