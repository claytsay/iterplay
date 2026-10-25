"""Turning evaluation results into the Fixer's work queue (HLD §4 [2], [8])."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from iterplay.models.critique import EvidenceType, Issue, Severity
from iterplay.models.rubric import RubricItemId
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)

DEFAULT_QUEUE_LIMIT: Final = 5
"""Issues per fix, so the Fixer stays focused (HLD §5)."""

_QUOTED_ERRORS: Final = 3
"""Errors quoted in each smoke-failure issue; the rest are only counted."""

_ERROR_CHARS: Final = 500
"""Longest error message quoted in a smoke-failure issue."""

_SEVERITY_RANK: Final[Mapping[Severity, int]] = MappingProxyType(
    {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
)

_STATUS_RANK: Final[Mapping[VerificationStatus, int]] = MappingProxyType(
    {VerificationStatus.CONFIRMED: 0, VerificationStatus.UNVERIFIABLE: 1}
)
"""Statuses that reach the Fixer, in priority order. Contradicted issues never do."""

_CHECK_FAILURES: Final[Mapping[SmokeCheck, tuple[str, str]]] = MappingProxyType(
    {
        SmokeCheck.READY: (
            "window.iterplay.ready did not become true in time.",
            "Set window.iterplay.ready = true once the scene is loaded and playable, and make "
            "sure nothing throws during start-up.",
        ),
        SmokeCheck.STATE_SCHEMA: (
            "getState() did not return data matching the contract schema.",
            "Return every getState() field with the documented name and type, and never throw.",
        ),
        SmokeCheck.NO_ERRORS: (
            "The page threw or logged errors.",
            "Fix the code behind these errors: the game must load and run without any.",
        ),
        SmokeCheck.PLAYER_MOVES: (
            "Holding the move key did not change the player's position.",
            "Make the movement keys move the player, and report the new position in getState().",
        ),
    }
)
"""What each failed check means when the smoke test gives no detail, and how to fix it."""

_UNNAMED_FAILURE: Final = (
    "The smoke test failed.",
    "Make the game load and run without errors, and honour the harness contract.",
)


def smoke_failure_issues(report: SmokeReport) -> list[VerifiedIssue]:
    """One confirmed, critical ``boots_cleanly`` issue per failed check, with its errors.

    Issues are numbered ``B1``, ``B2``, ... in check order. A report that failed without
    naming a failed check still yields one issue, so a build failure always reaches the Fixer.
    """
    failures = [_failure(result) for result in report.checks if not result.passed]
    if not failures and not report.passed:
        failures = [_UNNAMED_FAILURE]
    errors = _quoted_errors(report)
    return [
        _smoke_issue(f"B{number}", observation + errors, fix_hint, report.attempt)
        for number, (observation, fix_hint) in enumerate(failures, start=1)
    ]


def fixer_queue(
    verified: VerifiedCritique, limit: int | None = DEFAULT_QUEUE_LIMIT
) -> list[VerifiedIssue]:
    """Issues for the next fix, most important first.

    Confirmed issues (critic and deterministic) come first, ordered by severity, followed by
    unverifiable ones, also ordered by severity. Issues of equal rank keep their order.
    Contradicted issues are never included. ``limit`` caps the queue; None means no cap.

    Raises:
        ValueError: ``limit`` is negative.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"The queue limit must be at least 0, not {limit}")
    ranked = sorted(
        (issue for issue in verified.issues if issue.status in _STATUS_RANK),
        key=lambda issue: (_STATUS_RANK[issue.status], _SEVERITY_RANK[issue.issue.severity]),
    )
    return ranked if limit is None else ranked[:limit]


def _failure(result: SmokeCheckResult) -> tuple[str, str]:
    """Observation and fix hint for one failed check."""
    meaning, fix_hint = _CHECK_FAILURES.get(result.check, _UNNAMED_FAILURE)
    observation = f"Smoke check {result.check.value} failed: {_sentence(result.detail) or meaning}"
    return observation, fix_hint


def _smoke_issue(issue_id: str, observation: str, fix_hint: str, attempt: int) -> VerifiedIssue:
    issue = Issue(
        id=issue_id,
        rubric_item=RubricItemId.BOOTS_CLEANLY.value,
        severity=Severity.CRITICAL,
        observation=observation,
        evidence_type=EvidenceType.CONSOLE,
        fix_hint=fix_hint,
    )
    return VerifiedIssue(
        issue=issue,
        origin=IssueOrigin.SMOKE,
        status=VerificationStatus.CONFIRMED,
        rationale=f"Reported by the smoke test on attempt {attempt}.",
    )


def _quoted_errors(report: SmokeReport) -> str:
    """The first few distinct errors, uncaught before console, as a sentence to append; or ""."""
    errors = list(dict.fromkeys([*report.uncaught_errors, *report.console_errors]))
    if not errors:
        return ""
    quoted = "; ".join(_quote(error) for error in errors[:_QUOTED_ERRORS])
    rest = len(errors) - _QUOTED_ERRORS
    more = f" (and {rest} more)" if rest > 0 else ""
    return f" Errors: {quoted}{more}."


def _sentence(text: str) -> str:
    """``text`` stripped, ending in a full stop; "" stays empty."""
    text = text.strip()
    return text if not text or text.endswith((".", "!", "?")) else f"{text}."


def _quote(error: str) -> str:
    """``error`` on one line, shortened to ``_ERROR_CHARS``."""
    error = " ".join(error.split())
    return error if len(error) <= _ERROR_CHARS else error[: _ERROR_CHARS - 3].rstrip() + "..."
