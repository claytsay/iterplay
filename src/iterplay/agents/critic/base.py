"""Critic interface (HLD §4 [5], §5)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from iterplay.models.critique import Critique
from iterplay.models.media import PreparedMedia
from iterplay.models.rubric import Rubric
from iterplay.models.telemetry import TelemetrySummary


@dataclass(frozen=True, slots=True)
class CriticInput:
    design_doc: str
    rubric: Rubric
    media: PreparedMedia

    telemetry: TelemetrySummary
    """Pre-computed digest; the critic never sees raw JSONL."""

    console_errors: tuple[str, ...]


class CriticBackend(ABC):
    """Watches gameplay footage and reports issues against the design doc and rubric.

    Its output is unverified: the Verifier checks every claim before the Fixer sees it.
    """

    @abstractmethod
    async def critique(self, request: CriticInput) -> Critique:
        """Return the critic's raw findings, persisted as ``critique.raw.json``.

        Raises:
            LLMError: The model call failed or returned invalid JSON.
        """
