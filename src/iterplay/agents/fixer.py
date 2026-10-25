"""[8] Fixer: the model patches the game (HLD §4 [8])."""

from dataclasses import dataclass

from iterplay.agents.guardrails import GuardrailResult
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.llm.base import LLMClient
from iterplay.models.game import ChangelogEntry, GameBundle
from iterplay.models.run import VersionOutcome
from iterplay.models.verification import VerifiedIssue


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """What an earlier version tried and how its score moved, for the Fixer's history."""

    version: int
    parent_version: int | None
    score: float

    score_delta: float | None
    """Change versus the parent version; None for v1."""

    outcome: VersionOutcome
    changes: tuple[ChangelogEntry, ...]


@dataclass(frozen=True, slots=True)
class RejectedAttempt:
    """A regressed version, attached as "what not to do" after a revert (HLD §4 [7])."""

    attempt: AttemptSummary

    issues: tuple[VerifiedIssue, ...]
    """Issues found in the regressed version."""


@dataclass(frozen=True, slots=True)
class FixRequest:
    design_doc: str

    current: GameBundle
    """Files to start from: the latest version, or the best one after a regression."""

    issues: tuple[VerifiedIssue, ...]
    """Ranked work queue; see :func:`iterplay.evaluation.issues.fixer_queue`."""

    history: tuple[AttemptSummary, ...] = ()
    rejected_attempt: RejectedAttempt | None = None


@dataclass(frozen=True, slots=True)
class FixResult:
    files: GuardrailResult

    changelog: tuple[ChangelogEntry, ...]
    """Patch notes for the new version; every entry cites issue ids."""


class Fixer:
    """Asks the model for a complete replacement file set plus a changelog.

    Full rewrites rather than diffs, in the same format as the Builder. The game contract is
    repeated at the end of every prompt.
    """

    def __init__(self, llm: LLMClient, prompts: PromptLibrary) -> None:
        self._llm = llm
        self._prompts = prompts

    async def fix(self, request: FixRequest) -> FixResult:
        """Produce the next version.

        Raises:
            LLMError: The model call failed or returned invalid JSON.
            GuardrailError: The returned file set is unusable.
        """
        raise NotImplementedError
