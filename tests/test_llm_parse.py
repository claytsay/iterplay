"""parse_response validates model output and explains failures so the model can fix them."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from iterplay.errors import LLMError, LLMResponseError
from iterplay.llm.base import LLMResponse, parse_response
from iterplay.models.critique import Critique, Severity
from iterplay.models.game import BuilderResponse
from iterplay.models.run import TokenUsage


def _response(text: str) -> LLMResponse:
    return LLMResponse(text=text, usage=TokenUsage(input_tokens=10, output_tokens=5))


def _error_for(text: str) -> LLMResponseError:
    with pytest.raises(LLMResponseError) as caught:
        parse_response(_response(text), Critique)
    return caught.value


def test_plain_json_is_validated(hld_critique: dict[str, Any]) -> None:
    critique = parse_response(_response(json.dumps(hld_critique)), Critique)

    assert critique == Critique.model_validate(hld_critique)
    assert critique.issues[0].severity is Severity.HIGH


@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\nBODY\n```",
        "```JSON\nBODY\n```",
        "```\nBODY\n```",
        "\n  ```json\nBODY\n```  \n",
        "```json\r\nBODY\r\n```",
        "```json BODY```",
        "```BODY```",
    ],
)
def test_a_surrounding_code_fence_is_stripped(wrapper: str, hld_critique: dict[str, Any]) -> None:
    text = wrapper.replace("BODY", json.dumps(hld_critique, indent=2))

    assert parse_response(_response(text), Critique) == Critique.model_validate(hld_critique)


def test_backticks_inside_fenced_json_survive() -> None:
    content = "// Usage:\n// ```js\n// start();\n// ```\nconst s = `a ${1} b`;\n"
    body = json.dumps({"files": [{"path": "main.js", "content": content}]})

    bundle = parse_response(_response(f"```json\n{body}\n```"), BuilderResponse)

    assert bundle.files[0].content == content


def test_unfenced_json_is_left_alone() -> None:
    content = "```\n```"
    body = json.dumps({"files": [{"path": "main.js", "content": content}]})

    assert parse_response(_response(body), BuilderResponse).files[0].content == content


def test_an_unclosed_fence_is_not_json() -> None:
    error = _error_for('```json\n{"issues": []}')

    assert "Invalid JSON" in str(error)


@pytest.mark.parametrize("text", ["", "   ", "not json", '{"issues": [', "\xff"])
def test_invalid_json_raises_llm_response_error(text: str) -> None:
    error = _error_for(text)

    assert isinstance(error, LLMError)
    assert isinstance(error.__cause__, ValidationError)
    assert str(error).startswith("Response does not match Critique: Invalid JSON")


def test_violations_name_the_field_and_the_offending_value(hld_critique: dict[str, Any]) -> None:
    issue = {**hld_critique["issues"][0], "severity": "meh", "timestamp": "1:2"}
    del issue["observation"]
    payload = {**hld_critique, "issues": [issue], "rubric_assessment": {"feel": 9}}

    message = str(_error_for(json.dumps(payload)))

    assert "issues[0].severity: Input should be " in message
    assert '(got "meh")' in message
    assert "issues[0].timestamp: String should match pattern" in message
    assert "issues[0].observation: Field required" in message
    assert "rubric_assessment.feel: Input should be less than or equal to 5 (got 9)" in message


def test_a_wrong_top_level_type_has_no_location() -> None:
    message = str(_error_for("[1, 2]"))

    assert message == "Response does not match Critique: Input should be an object"


def test_at_most_five_problems_are_listed() -> None:
    payload = {"issues": [{"id": f"I{n}"} for n in range(4)]}

    message = str(_error_for(json.dumps(payload)))

    assert message.count("Field required") == 5
    assert message.endswith("; and 11 more")


def test_summary_and_values_are_truncated() -> None:
    issue = {
        "id": "I1",
        "rubric_item": "x" * 5000,
        "severity": "s" * 5000,
        "observation": "o",
        "evidence_type": "visual",
    }
    payload = {"issues": [issue], "rubric_assessment": {"k" * 5000: "bad"}}

    message = str(_error_for(json.dumps(payload)))

    assert len(message) <= 800
    assert message.endswith("...")
    assert '(got "sssss' in message
    assert "s" * 100 not in message
