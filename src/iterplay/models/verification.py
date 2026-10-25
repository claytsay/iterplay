"""Verifier output (HLD §4 [6])."""

from enum import StrEnum

from pydantic import Field

from iterplay.models.base import StrictModel
from iterplay.models.critique import AssessmentScore, Issue


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    """Telemetry or console output corroborates the claim."""

    UNVERIFIABLE = "unverifiable"
    """Telemetry cannot settle the claim (e.g. subjective feel); kept at lower priority."""

    CONTRADICTED = "contradicted"
    """Telemetry disproves the claim; logged as a caught hallucination, never fixed."""


class IssueOrigin(StrEnum):
    CRITIC = "critic"

    DETERMINISTIC = "deterministic"
    """Found by the Verifier itself: uncaught errors, unreachable collectibles, stuck bots."""

    SMOKE = "smoke"
    """A failed smoke check, reported to the Fixer as a build failure."""


class VerifiedIssue(StrictModel):
    issue: Issue
    origin: IssueOrigin
    status: VerificationStatus
    rationale: str = Field(description="Evidence behind the status, for the viewer and Fixer.")


class VerifiedCritique(StrictModel):
    """Contents of ``critique.verified.json``."""

    issues: list[VerifiedIssue] = Field(description="Every issue, contradicted ones included.")
    feel_notes: list[str] = Field(default_factory=list)
    rubric_assessment: dict[str, AssessmentScore] = Field(default_factory=dict)
