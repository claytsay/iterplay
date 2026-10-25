"""The Fixer turns the current files, ranked issues and history into the next version
(HLD §4 [7], [8])."""

import json
import logging
import re
from typing import Any

import pytest

from iterplay.agents.fixer import AttemptSummary, Fixer, FixRequest, RejectedAttempt
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.contract.state import GameState
from iterplay.contract.templates import TEMPLATES, TemplateId
from iterplay.errors import GuardrailError, LLMError, LLMResponseError
from iterplay.models.critique import EvidenceType, Issue, Severity
from iterplay.models.game import ChangelogEntry, FixerResponse, GameBundle, GameFile
from iterplay.models.playtest import BotName
from iterplay.models.run import VersionOutcome
from iterplay.models.verification import IssueOrigin, VerificationStatus, VerifiedIssue
from tests.helpers import FakeLLMClient

TEMPLATE = TEMPLATES[TemplateId.PLATFORMER_2D]

DESIGN_DOC = "# Gem Dash\nCollect three gems, then touch the flag. Each gem is worth $5."

INDEX_HTML = GameFile(
    path="index.html", content='<!doctype html>\n<script type="module" src="./main.js"></script>\n'
)
MAIN_JS = GameFile(path="main.js", content="const label = `gems: ${count}`;\nstart();")
CURRENT = GameBundle(files=[INDEX_HTML, MAIN_JS])

PLACEHOLDER = re.compile(
    r"\$\{?(?:design_doc|files|issues|history|rejected_attempt|style_guide|contract)\b"
)


def verified(
    issue_id: str,
    *,
    status: VerificationStatus = VerificationStatus.CONFIRMED,
    origin: IssueOrigin = IssueOrigin.CRITIC,
    **fields: Any,
) -> VerifiedIssue:
    issue = Issue.model_validate(
        {
            "id": issue_id,
            "rubric_item": "collision_integrity",
            "severity": "high",
            "observation": "The player falls through the second wall.",
            "evidence_type": "visual+telemetry",
            **fields,
        }
    )
    return VerifiedIssue(
        issue=issue, origin=origin, status=status, rationale=f"Evidence for {issue_id}."
    )


WALL = verified(
    "I1",
    bot=BotName.SPEEDRUNNER,
    timestamp="00:42",
    suspected_cause="The wall has no collider.",
    fix_hint="Add the wall to the colliders.",
)
GEM = verified(
    "D1",
    origin=IssueOrigin.DETERMINISTIC,
    rubric_item="collectibles_reachable",
    severity=Severity.MEDIUM,
    evidence_type=EvidenceType.TELEMETRY,
    observation="No bot collected gem-3.",
)
FEEL = verified(
    "I2",
    status=VerificationStatus.UNVERIFIABLE,
    rubric_item="game_feel",
    severity=Severity.LOW,
    evidence_type=EvidenceType.VISUAL,
    observation="Jumps feel floaty.",
)

V1 = AttemptSummary(
    version=1,
    parent_version=None,
    score=0.42,
    score_delta=None,
    outcome=VersionOutcome.ACCEPTED,
    changes=(),
)
V2 = AttemptSummary(
    version=2,
    parent_version=1,
    score=0.555,
    score_delta=0.135,
    outcome=VersionOutcome.ACCEPTED,
    changes=(
        ChangelogEntry(issue_ids=["I1", "D2"], summary="Registered the walls as colliders."),
        ChangelogEntry(issue_ids=["I4"], summary="Shortened the\n  coyote time"),
    ),
)
V3 = AttemptSummary(
    version=3,
    parent_version=2,
    score=0.48,
    score_delta=-0.075,
    outcome=VersionOutcome.REVERTED,
    changes=(ChangelogEntry(issue_ids=["I2"], summary="Rewrote the physics step."),),
)


@pytest.fixture
def prompts() -> PromptLibrary:
    return PromptLibrary(TEMPLATE)


def reply(*files: GameFile, changelog: list[ChangelogEntry] | None = None) -> FixerResponse:
    entries = [ChangelogEntry(issue_ids=["I1"], summary="Added wall colliders.")]
    return FixerResponse(
        files=list(files) or [INDEX_HTML, MAIN_JS],
        changelog=entries if changelog is None else changelog,
    )


def fix_request(**changes: Any) -> FixRequest:
    fields: dict[str, Any] = {
        "design_doc": DESIGN_DOC,
        "current": CURRENT,
        "issues": (WALL, GEM, FEEL),
        **changes,
    }
    return FixRequest(**fields)


async def fixer_prompt(prompts: PromptLibrary, request: FixRequest | None = None) -> str:
    llm = FakeLLMClient({"fixer": [reply()]})
    await Fixer(llm, prompts).fix(request or fix_request())
    [sent] = llm.requests
    return sent.prompt


def section(prompt: str, heading: str) -> str:
    """The text under ``## <heading>``, up to the next level-2 heading."""
    start = prompt.index(f"\n## {heading}\n") + len(heading) + 5
    end = prompt.find("\n## ", start)
    return prompt[start:end].strip()


def json_block(text: str) -> Any:
    match = re.search(r"^```json\n(.*?)\n```$", text, re.DOTALL | re.MULTILINE)
    assert match is not None, text
    return json.loads(match.group(1))


async def test_the_request_asks_for_a_fixer_response(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"fixer": [reply()]})

    await Fixer(llm, prompts).fix(fix_request())

    [request] = llm.requests
    assert request.purpose == "fixer"
    assert request.response_schema is FixerResponse
    assert request.media == ()
    assert "browser-game developer" in request.system_instruction
    assert "Respond only with JSON" in request.system_instruction


async def test_every_placeholder_is_filled(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts)

    assert not PLACEHOLDER.search(prompt)
    assert section(prompt, "Design doc") == DESIGN_DOC


async def test_the_style_guide_and_contract_close_the_prompt(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts)
    schema = json.dumps(GameState.model_json_schema(by_alias=True), indent=2)

    assert prompts.render_style_guide() in prompt
    assert prompt.rstrip().endswith(prompts.render_contract().rstrip())
    assert schema in prompt


async def test_the_prompt_is_the_rendered_fixer_template(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts)
    values = {
        name: section(prompt, heading)
        for name, heading in [
            ("files", "Current files"),
            ("history", "Earlier attempts in this run"),
            ("rejected_attempt", "What not to do"),
        ]
    }
    issues = section(prompt, "Issues to fix, highest priority first").split("\n\n", 1)[1]

    assert prompt == prompts.render(
        PromptName.FIXER,
        design_doc=DESIGN_DOC,
        issues=issues,
        style_guide=prompts.render_style_guide(),
        contract=prompts.render_contract(),
        **values,
    )


async def test_each_current_file_gets_a_heading_and_a_code_block(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts)

    assert section(prompt, "Current files") == (
        "### index.html\n"
        "```html\n"
        '<!doctype html>\n<script type="module" src="./main.js"></script>\n'
        "```\n"
        "\n"
        "### main.js\n"
        "```js\n"
        "const label = `gems: ${count}`;\nstart();\n"
        "```"
    )


async def test_a_file_containing_a_code_fence_gets_a_longer_fence(prompts: PromptLibrary) -> None:
    notes = GameFile(path="notes.js", content="// Usage:\n// ````js\n// start();\n// ````\n")
    current = GameBundle(files=[INDEX_HTML, MAIN_JS, notes])

    prompt = await fixer_prompt(prompts, fix_request(current=current))

    assert f"### notes.js\n`````js\n{notes.content}`````" in prompt


async def test_issues_are_compact_json_in_queue_order(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts)
    text = section(prompt, "Issues to fix, highest priority first")

    assert json_block(text) == [
        {
            "id": "I1",
            "severity": "high",
            "rubric_item": "collision_integrity",
            "bot": "speedrunner",
            "timestamp": "00:42",
            "observation": "The player falls through the second wall.",
            "suspected_cause": "The wall has no collider.",
            "fix_hint": "Add the wall to the colliders.",
            "status": "confirmed",
            "rationale": "Evidence for I1.",
        },
        {
            "id": "D1",
            "severity": "medium",
            "rubric_item": "collectibles_reachable",
            "observation": "No bot collected gem-3.",
            "status": "confirmed",
            "rationale": "Evidence for D1.",
        },
        {
            "id": "I2",
            "severity": "low",
            "rubric_item": "game_feel",
            "observation": "Jumps feel floaty.",
            "status": "unverifiable",
            "rationale": "Evidence for I2.",
        },
    ]
    assert '\n{"id": "D1", "severity": "medium", ' in text


async def test_an_empty_queue_says_none(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts, fix_request(issues=()))

    assert section(prompt, "Issues to fix, highest priority first").endswith("\n\nNone.")


async def test_without_history_or_a_rejected_attempt_both_sections_say_none(
    prompts: PromptLibrary,
) -> None:
    prompt = await fixer_prompt(prompts)

    assert section(prompt, "Earlier attempts in this run") == "None."
    assert section(prompt, "What not to do") == "None."


async def test_history_has_one_line_per_attempt(prompts: PromptLibrary) -> None:
    prompt = await fixer_prompt(prompts, fix_request(history=(V1, V2, V3)))

    assert section(prompt, "Earlier attempts in this run").splitlines() == [
        "- v1 (first build): scored 42.0%, accepted.",
        "- v2 (from v1): scored 55.5% (+13.5 points vs v1), accepted. Changes: [I1, D2] "
        "Registered the walls as colliders; [I4] Shortened the coyote time.",
        "- v3 (from v2): scored 48.0% (-7.5 points vs v2), reverted because it scored below "
        "the best version. Changes: [I2] Rewrote the physics step.",
    ]


async def test_a_build_failure_is_described_as_such(prompts: PromptLibrary) -> None:
    failed = AttemptSummary(
        version=4,
        parent_version=3,
        score=0.0,
        score_delta=-0.48,
        outcome=VersionOutcome.BUILD_FAILED,
        changes=(),
    )

    prompt = await fixer_prompt(prompts, fix_request(history=(failed,)))

    assert section(prompt, "Earlier attempts in this run") == (
        "- v4 (from v3): scored 0.0% (-48.0 points vs v3), discarded because it failed the "
        "smoke test."
    )


async def test_the_rejected_attempt_says_what_not_to_do(prompts: PromptLibrary) -> None:
    regression = verified("D1", origin=IssueOrigin.DETERMINISTIC, observation="Fell at 00:12.")
    rejected = RejectedAttempt(attempt=V3, issues=(regression,))

    prompt = await fixer_prompt(prompts, fix_request(rejected_attempt=rejected))
    text = section(prompt, "What not to do")

    assert text.startswith(
        "v3 (from v2): scored 48.0% (-7.5 points vs v2), reverted because it scored below the "
        "best version. Changes: [I2] Rewrote the physics step.\n"
        "Its changes made the game worse, so the current files do not include them. Avoid "
        "repeating its mistakes. These issues were found in v3:\n\n"
    )
    assert json_block(text) == [
        {
            "id": "D1",
            "severity": "high",
            "rubric_item": "collision_integrity",
            "observation": "Fell at 00:12.",
            "status": "confirmed",
            "rationale": "Evidence for D1.",
        }
    ]


async def test_the_result_carries_the_guarded_files_and_changelog(prompts: PromptLibrary) -> None:
    player = GameFile(path="player.js", content="export const speed = 7;\n")
    changelog = [
        ChangelogEntry(issue_ids=["I1"], summary="Added wall colliders."),
        ChangelogEntry(issue_ids=["D1", "I2"], summary="Lowered gem-3 and snappier jumps."),
    ]
    files = [player, MAIN_JS, INDEX_HTML, GameFile(path="levels/one.js", content="")]
    llm = FakeLLMClient({"fixer": [reply(*files, changelog=changelog)]})

    result = await Fixer(llm, prompts).fix(fix_request())

    assert result.files.bundle.files == [INDEX_HTML, MAIN_JS, player]
    assert result.files.rejected_paths == ("levels/one.js",)
    assert result.changelog == tuple(changelog)


async def test_unknown_issue_ids_are_kept_and_logged(
    prompts: PromptLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="iterplay.agents.fixer")
    changelog = [
        ChangelogEntry(issue_ids=["I1", "X9"], summary="Added wall colliders."),
        ChangelogEntry(issue_ids=["Q1", "X9"], summary="Tuned the jump."),
    ]
    llm = FakeLLMClient({"fixer": [reply(changelog=changelog)]})

    result = await Fixer(llm, prompts).fix(fix_request())

    assert result.changelog == tuple(changelog)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "The fixer's changelog cites unknown issue ids, kept as they are: X9, Q1"
    )


async def test_known_issue_ids_log_nothing(
    prompts: PromptLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="iterplay.agents.fixer")
    changelog = [ChangelogEntry(issue_ids=["I1", "D1", "I2"], summary="Fixed all three.")]
    llm = FakeLLMClient({"fixer": [reply(changelog=changelog)]})

    await Fixer(llm, prompts).fix(fix_request())

    assert caplog.records == []


async def test_invalid_json_is_retried_once_with_the_error(prompts: PromptLibrary) -> None:
    missing_changelog = json.dumps({"files": [INDEX_HTML.model_dump(), MAIN_JS.model_dump()]})
    llm = FakeLLMClient({"fixer": [missing_changelog, reply()]})

    result = await Fixer(llm, prompts).fix(fix_request())

    first, retry = llm.requests
    assert retry.prompt.startswith(first.prompt)
    assert retry.prompt.removeprefix(first.prompt).startswith(
        "\n\n## Your previous response was rejected\n"
        "Response does not match FixerResponse: changelog: Field required"
    )
    assert retry.purpose == "fixer"
    assert result.changelog == tuple(reply().changelog)


async def test_a_guardrail_failure_is_retried_once_with_the_problem(
    prompts: PromptLibrary,
) -> None:
    llm = FakeLLMClient({"fixer": [reply(INDEX_HTML), reply()]})

    result = await Fixer(llm, prompts).fix(fix_request())

    first, retry = llm.requests
    assert "Missing main.js" in retry.prompt.removeprefix(first.prompt)
    assert result.files.bundle.files == [INDEX_HTML, MAIN_JS]


async def test_a_second_invalid_response_propagates(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"fixer": ["{}", "[]"]})

    with pytest.raises(LLMResponseError, match=r"^Response does not match FixerResponse"):
        await Fixer(llm, prompts).fix(fix_request())

    assert len(llm.requests) == 2


async def test_a_second_guardrail_failure_propagates(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"fixer": [reply(INDEX_HTML), reply(MAIN_JS)]})

    with pytest.raises(GuardrailError, match=r"Missing index\.html"):
        await Fixer(llm, prompts).fix(fix_request())

    assert len(llm.requests) == 2


async def test_a_failed_model_call_is_not_retried(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"fixer": [LLMError("Gemini is overloaded"), reply()]})

    with pytest.raises(LLMError, match="overloaded"):
        await Fixer(llm, prompts).fix(fix_request())

    assert len(llm.requests) == 1
