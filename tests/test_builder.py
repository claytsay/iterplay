"""The Builder turns a design doc into a guarded v1 file set (HLD §4 [1])."""

import json
import logging
import re

import pytest

from iterplay.agents.builder import Builder
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.contract.state import GameState
from iterplay.contract.templates import TEMPLATES, TemplateId
from iterplay.errors import BudgetExceededError, GuardrailError, LLMError, LLMResponseError
from iterplay.llm.base import LLMRequest
from iterplay.models.game import BuilderResponse, GameFile
from tests.helpers import FakeLLMClient

TEMPLATE = TEMPLATES[TemplateId.PLATFORMER_2D]

DESIGN_DOC = "# Gem Dash\nCollect three gems, then touch the flag. Each gem is worth $5."

INDEX_HTML = GameFile(
    path="index.html", content='<!doctype html>\n<script type="module" src="./main.js"></script>\n'
)
MAIN_JS = GameFile(
    path="main.js", content='import * as THREE from "./vendor/three.module.js";\nconst s = `$`;\n'
)
PLACEHOLDER = re.compile(
    r"\$\{?(?:game_kind|design_doc|style_guide|contract|state_schema|kill_plane_y)\b"
)


@pytest.fixture
def prompts() -> PromptLibrary:
    return PromptLibrary(TEMPLATE)


def game(*files: GameFile) -> BuilderResponse:
    return BuilderResponse(files=[MAIN_JS, INDEX_HTML, *files])


def html_only() -> BuilderResponse:
    return BuilderResponse(files=[INDEX_HTML])


async def build(
    prompts: PromptLibrary, *replies: BuilderResponse | str | Exception
) -> FakeLLMClient:
    llm = FakeLLMClient({"builder": list(replies)})
    await Builder(llm, prompts).build(DESIGN_DOC)
    return llm


async def only_request(prompts: PromptLibrary) -> LLMRequest:
    llm = await build(prompts, game())
    [request] = llm.requests
    return request


async def test_the_prompt_fills_every_builder_placeholder(prompts: PromptLibrary) -> None:
    request = await only_request(prompts)

    assert request.prompt == prompts.render(
        PromptName.BUILDER,
        game_kind=TEMPLATE.description,
        design_doc=DESIGN_DOC,
        style_guide=prompts.render_style_guide(),
        contract=prompts.render_contract(),
    )
    assert not PLACEHOLDER.search(request.prompt)


async def test_the_prompt_names_the_game_and_includes_the_design_doc_verbatim(
    prompts: PromptLibrary,
) -> None:
    request = await only_request(prompts)

    assert f"playable {TEMPLATE.description} with Three.js" in request.prompt
    assert DESIGN_DOC in request.prompt
    assert prompts.render_style_guide() in request.prompt


async def test_the_contract_and_state_schema_close_the_prompt(prompts: PromptLibrary) -> None:
    request = await only_request(prompts)
    schema = json.dumps(GameState.model_json_schema(by_alias=True), indent=2)

    assert request.prompt.rstrip().endswith(prompts.render_contract().rstrip())
    assert schema in request.prompt
    assert "y = -20" in request.prompt


async def test_the_request_asks_for_a_builder_response(prompts: PromptLibrary) -> None:
    request = await only_request(prompts)

    assert request.purpose == "builder"
    assert request.response_schema is BuilderResponse
    assert request.media == ()
    assert "browser-game developer" in request.system_instruction
    assert "Respond only with JSON" in request.system_instruction


async def test_the_files_come_back_through_the_guardrails(prompts: PromptLibrary) -> None:
    player = GameFile(path="player.js", content="export const speed = 7;\n")
    reply = game(GameFile(path="../escape.js", content="x"), player)
    llm = FakeLLMClient({"builder": [reply]})

    result = await Builder(llm, prompts).build(DESIGN_DOC)

    assert result.bundle.files == [INDEX_HTML, MAIN_JS, player]
    assert result.rejected_paths == ("../escape.js",)


async def test_discarded_paths_are_logged(
    prompts: PromptLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="iterplay.agents.builder")

    await build(prompts, game(GameFile(path="vendor/three.module.js", content="")))

    assert "Discarded builder files outside the allowlist: 'vendor/three.module.js'" in caplog.text


async def test_a_response_in_a_code_fence_is_accepted(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"builder": [f"```json\n{game().model_dump_json()}\n```"]})

    result = await Builder(llm, prompts).build(DESIGN_DOC)

    assert [file.path for file in result.bundle.files] == ["index.html", "main.js"]


async def test_invalid_json_is_retried_once_with_the_error_appended(
    prompts: PromptLibrary,
) -> None:
    llm = FakeLLMClient({"builder": ['{"files": "index.html"}', game()]})

    result = await Builder(llm, prompts).build(DESIGN_DOC)

    first, retry = llm.requests
    feedback = retry.prompt.removeprefix(first.prompt)
    assert feedback != retry.prompt
    assert feedback.startswith(
        "\n\n## Your previous response was rejected\n"
        "Response does not match BuilderResponse: files: Input should be a valid array"
    )
    assert retry.purpose == first.purpose
    assert retry.response_schema is BuilderResponse
    assert retry.system_instruction == first.system_instruction
    assert [file.path for file in result.bundle.files] == ["index.html", "main.js"]


async def test_a_guardrail_failure_is_retried_once_with_the_problem_appended(
    prompts: PromptLibrary,
) -> None:
    llm = FakeLLMClient({"builder": [html_only(), game()]})

    result = await Builder(llm, prompts).build(DESIGN_DOC)

    first, retry = llm.requests
    assert retry.prompt.startswith(first.prompt)
    assert "Missing main.js. Every response must include index.html and main.js" in retry.prompt
    assert result.bundle.files == [INDEX_HTML, MAIN_JS]


async def test_a_second_invalid_response_propagates(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"builder": ["not json", "still not json"]})

    with pytest.raises(LLMResponseError, match=r"^Response does not match BuilderResponse"):
        await Builder(llm, prompts).build(DESIGN_DOC)

    assert len(llm.requests) == 2


async def test_a_second_guardrail_failure_propagates(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"builder": [html_only(), html_only()]})

    with pytest.raises(GuardrailError, match=r"Missing main\.js"):
        await Builder(llm, prompts).build(DESIGN_DOC)

    assert len(llm.requests) == 2


async def test_the_retry_reports_its_own_problem(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"builder": ["not json", html_only()]})

    with pytest.raises(GuardrailError, match=r"Missing main\.js"):
        await Builder(llm, prompts).build(DESIGN_DOC)


@pytest.mark.parametrize(
    "error", [LLMError("Gemini is overloaded"), BudgetExceededError("Budget exhausted")]
)
async def test_failed_model_calls_are_not_retried(prompts: PromptLibrary, error: Exception) -> None:
    llm = FakeLLMClient({"builder": [error, game()]})

    with pytest.raises(type(error)):
        await Builder(llm, prompts).build(DESIGN_DOC)

    assert len(llm.requests) == 1


async def test_a_failed_retry_call_propagates(prompts: PromptLibrary) -> None:
    llm = FakeLLMClient({"builder": ["not json", LLMError("connection reset")]})

    with pytest.raises(LLMError, match="connection reset"):
        await Builder(llm, prompts).build(DESIGN_DOC)


async def test_the_retry_is_logged_as_a_warning(
    prompts: PromptLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="iterplay.agents.builder")

    await build(prompts, html_only(), game())

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Retrying the rejected builder response once: Missing")
