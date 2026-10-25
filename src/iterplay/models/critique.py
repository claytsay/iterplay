"""Critic output (HLD §4 [5]), which is also the response schema the critic must follow."""

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from iterplay.models.base import TolerantModel
from iterplay.models.playtest import BotName

TIMESTAMP_PATTERN: Final = r"^\d{2}:\d{2}$"
"""``mm:ss``, as burned into each bot's clip."""

AssessmentScore = Annotated[int, Field(ge=0, le=5)]


class Severity(StrEnum):
    """How badly an issue hurts the game; ranks the Fixer's work queue."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceType(StrEnum):
    """What the critic based a claim on."""

    VISUAL = "visual"
    TELEMETRY = "telemetry"
    VISUAL_TELEMETRY = "visual+telemetry"
    CONSOLE = "console"


class Issue(TolerantModel):
    """A problem with the game, in the shape the critic reports it."""

    id: str = Field(description="Unique within a version, e.g. 'I3'.")
    bot: BotName | None = Field(default=None, description="Run the issue was seen in, if any.")
    timestamp: str | None = Field(
        default=None,
        pattern=TIMESTAMP_PATTERN,
        description="mm:ss on that bot's burned-in clock.",
    )
    rubric_item: str = Field(description="Id of the rubric item the issue affects.")
    severity: Severity
    observation: str
    evidence_type: EvidenceType
    suspected_cause: str = ""
    fix_hint: str = ""


class Critique(TolerantModel):
    """Contents of ``critique.raw.json``: exactly what the critic said."""

    issues: list[Issue]
    feel_notes: list[str] = Field(default_factory=list)
    rubric_assessment: dict[str, AssessmentScore] = Field(
        default_factory=dict,
        description="0-5 score for each critic-sourced rubric item, keyed by item id.",
    )
