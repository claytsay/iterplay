"""Turning evaluation results into the Fixer's work queue (HLD §4 [2], [8])."""

from typing import Final

from iterplay.models.smoke import SmokeReport
from iterplay.models.verification import VerifiedCritique, VerifiedIssue

DEFAULT_QUEUE_LIMIT: Final = 5
"""Issues per fix, so the Fixer stays focused (HLD §5)."""


def smoke_failure_issues(report: SmokeReport) -> list[VerifiedIssue]:
    """One confirmed, critical ``boots_cleanly`` issue per failed check, with its errors."""
    raise NotImplementedError


def fixer_queue(
    verified: VerifiedCritique, limit: int | None = DEFAULT_QUEUE_LIMIT
) -> list[VerifiedIssue]:
    """Issues for the next fix, most important first.

    Confirmed issues (critic and deterministic) come first, ordered by severity, followed by
    unverifiable ones. Contradicted issues are never included.
    """
    raise NotImplementedError
