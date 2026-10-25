"""[8] Fixer: the model patches the game (HLD §4 [8])."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final

from iterplay.agents.builder import generate_files
from iterplay.agents.guardrails import GuardrailResult
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.llm.base import LLMClient
from iterplay.models.game import ChangelogEntry, FixerResponse, GameBundle, GameFile
from iterplay.models.run import VersionOutcome
from iterplay.models.verification import VerifiedIssue

_NONE: Final = "None."
"""What an empty prompt section says."""

_FENCE_LANGUAGES: Final[Mapping[str, str]] = MappingProxyType({".html": "html", ".js": "js"})

_OUTCOMES: Final[Mapping[VersionOutcome, str]] = MappingProxyType(
    {
        VersionOutcome.ACCEPTED: "accepted",
        VersionOutcome.REVERTED: "reverted because it scored below the best version",
        VersionOutcome.BUILD_FAILED: "discarded because it failed the smoke test",
    }
)

_BACKTICK_RUN: Final = re.compile(r"`+")

_log = logging.getLogger(__name__)


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

        The prompt shows every current file, the issues as compact JSON, one line per
        earlier attempt, and the rejected attempt as "what not to do". Like the Builder, a
        response that is not valid JSON for the schema, or whose files break the guardrails,
        is retried once with the problem appended to the prompt. Changelog entries citing
        ids that are not in ``request.issues`` are kept, and logged as a warning.

        Raises:
            LLMError: The model call failed, or the retry returned invalid JSON.
            GuardrailError: The retry's file set is unusable.
        """
        prompts = self._prompts
        prompt = prompts.render(
            PromptName.FIXER,
            design_doc=request.design_doc,
            files=_render_files(request.current),
            issues=_render_issues(request.issues),
            history=_render_history(request.history),
            rejected_attempt=_render_rejected_attempt(request.rejected_attempt),
            style_guide=prompts.render_style_guide(),
            contract=prompts.render_contract(),
        )
        response, files = await generate_files(
            self._llm, purpose="fixer", prompt=prompt, schema=FixerResponse
        )
        changelog = tuple(response.changelog)
        _warn_about_unknown_ids(changelog, request.issues)
        return FixResult(files=files, changelog=changelog)


def _render_files(bundle: GameBundle) -> str:
    """Each file under a ``### <path>`` heading, in a fenced code block."""
    return "\n\n".join(_render_file(file) for file in bundle.files) or _NONE


def _render_file(file: GameFile) -> str:
    language = _FENCE_LANGUAGES.get(PurePosixPath(file.path).suffix, "")
    return f"### {file.path}\n{_fenced(file.content, language)}"


def _render_issues(issues: Sequence[VerifiedIssue]) -> str:
    """A JSON array with one compact issue object per line, in priority order."""
    if not issues:
        return _NONE
    rows = ",\n".join(json.dumps(_issue_fields(issue), ensure_ascii=False) for issue in issues)
    return _fenced(f"[\n{rows}\n]", "json")


def _issue_fields(verified: VerifiedIssue) -> dict[str, str]:
    """What the Fixer needs to know about an issue, leaving out empty fields."""
    issue = verified.issue
    fields = {
        "id": issue.id,
        "severity": issue.severity.value,
        "rubric_item": issue.rubric_item,
        "bot": None if issue.bot is None else issue.bot.value,
        "timestamp": issue.timestamp,
        "observation": issue.observation,
        "suspected_cause": issue.suspected_cause,
        "fix_hint": issue.fix_hint,
        "status": verified.status.value,
        "rationale": verified.rationale,
    }
    return {key: value for key, value in fields.items() if value}


def _render_history(history: Sequence[AttemptSummary]) -> str:
    return "\n".join(f"- {_describe_attempt(attempt)}" for attempt in history) or _NONE


def _render_rejected_attempt(rejected: RejectedAttempt | None) -> str:
    if rejected is None:
        return _NONE
    version = rejected.attempt.version
    return (
        f"{_describe_attempt(rejected.attempt)}\n"
        f"Its changes made the game worse, so the current files do not include them. Avoid "
        f"repeating its mistakes. These issues were found in v{version}:\n\n"
        f"{_render_issues(rejected.issues)}"
    )


def _describe_attempt(attempt: AttemptSummary) -> str:
    """One line: version, parent, score, change in score, outcome, and what was changed."""
    parent = attempt.parent_version
    origin = "first build" if parent is None else f"from v{parent}"
    line = f"v{attempt.version} ({origin}): scored {attempt.score:.1%}"
    if attempt.score_delta is not None:
        versus = "" if parent is None else f" vs v{parent}"
        line += f" ({attempt.score_delta * 100:+.1f} points{versus})"
    line += f", {_OUTCOMES[attempt.outcome]}."
    if attempt.changes:
        line += " Changes: " + "; ".join(_describe_change(entry) for entry in attempt.changes) + "."
    return line


def _describe_change(entry: ChangelogEntry) -> str:
    summary = " ".join(entry.summary.split()).rstrip(".")
    return f"[{', '.join(entry.issue_ids)}] {summary}"


def _fenced(text: str, language: str) -> str:
    """``text`` in a code fence longer than any run of backticks inside it."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text if text.endswith("\n") else f"{text}\n"
    return f"{fence}{language}\n{body}{fence}"


def _warn_about_unknown_ids(
    changelog: Sequence[ChangelogEntry], issues: Sequence[VerifiedIssue]
) -> None:
    known = {verified.issue.id for verified in issues}
    unknown = dict.fromkeys(
        issue_id for entry in changelog for issue_id in entry.issue_ids if issue_id not in known
    )
    if unknown:
        _log.warning(
            "The fixer's changelog cites unknown issue ids, kept as they are: %s",
            ", ".join(unknown),
        )
