"""Prompts load from package data, render strictly, and match what their callers fill in."""

import json
from importlib.resources import files
from string import Template

import pytest

from iterplay.agents import prompt_library
from iterplay.agents.prompt_library import TEMPLATE_PROMPTS, PromptLibrary, PromptName
from iterplay.contract.state import GameState
from iterplay.contract.templates import TEMPLATES, GameTemplate

PROMPTS = files("iterplay.agents") / "prompts"

PLACEHOLDERS = {
    PromptName.BUILDER: {"game_kind", "design_doc", "style_guide", "contract"},
    PromptName.CRITIC: {"design_doc", "rubric", "telemetry_summary", "console_errors"},
    PromptName.PERSONA_CRITIC: {
        "bot",
        "persona",
        "design_doc",
        "rubric",
        "telemetry_summary",
        "console_errors",
    },
    PromptName.LEAD_DESIGNER: {"max_issues", "rubric", "telemetry_summary", "critiques"},
    PromptName.FIXER: {
        "design_doc",
        "files",
        "issues",
        "history",
        "rejected_attempt",
        "style_guide",
        "contract",
    },
    PromptName.CONTRACT: {"state_schema", "kill_plane_y"},
    PromptName.STYLE_GUIDE: set(),
}
"""Exactly the values each prompt's caller must pass to ``render``."""

STATE_SCHEMA = json.dumps(GameState.model_json_schema(by_alias=True), indent=2)


@pytest.fixture(params=list(TEMPLATES.values()), ids=list(TEMPLATES))
def library(request: pytest.FixtureRequest) -> PromptLibrary:
    template: GameTemplate = request.param
    return PromptLibrary(template)


def placeholders(text: str) -> set[str]:
    return set(Template(text).get_identifiers())


def dummy_values(library: PromptLibrary, name: PromptName) -> dict[str, str]:
    return {key: f"<{key} value>" for key in placeholders(library.load(name))}


@pytest.mark.parametrize("name", sorted(set(PromptName) - TEMPLATE_PROMPTS))
def test_shared_prompts_load_from_the_prompts_directory(
    library: PromptLibrary, name: PromptName
) -> None:
    expected = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")

    assert library.load(name) == expected


@pytest.mark.parametrize("name", sorted(TEMPLATE_PROMPTS))
def test_template_prompts_load_from_the_template_directory(
    library: PromptLibrary, name: PromptName
) -> None:
    expected = (PROMPTS / library.template.id / f"{name}.md").read_text(encoding="utf-8")

    assert library.load(name) == expected


def test_every_prompt_has_documented_placeholders() -> None:
    assert set(PLACEHOLDERS) == set(PromptName)


@pytest.mark.parametrize("name", list(PromptName))
def test_prompts_use_exactly_the_documented_placeholders(
    library: PromptLibrary, name: PromptName
) -> None:
    assert placeholders(library.load(name)) == PLACEHOLDERS[name]


@pytest.mark.parametrize("name", list(PromptName))
def test_every_prompt_renders_once_its_placeholders_are_filled(
    library: PromptLibrary, name: PromptName
) -> None:
    assert Template(library.load(name)).is_valid()
    values = dummy_values(library, name)

    rendered = library.render(name, **values)

    assert "$" not in rendered
    assert all(value in rendered for value in values.values())


def test_a_missing_placeholder_raises_key_error(library: PromptLibrary) -> None:
    values = dummy_values(library, PromptName.BUILDER)
    del values["contract"]

    with pytest.raises(KeyError, match="contract"):
        library.render(PromptName.BUILDER, **values)


def test_values_are_inserted_verbatim(library: PromptLibrary) -> None:
    values = dummy_values(library, PromptName.CRITIC)
    values["design_doc"] = "Coins cost $5; see ${design_doc} and $$."

    rendered = library.render(PromptName.CRITIC, **values)

    assert "Coins cost $5; see ${design_doc} and $$." in rendered


def test_render_contract_fills_in_the_state_schema_and_kill_plane(library: PromptLibrary) -> None:
    contract = library.render_contract()

    assert contract == library.render(
        PromptName.CONTRACT, state_schema=STATE_SCHEMA, kill_plane_y="-20"
    )
    assert '"levelComplete"' in contract


def test_render_contract_keeps_the_digits_of_a_fractional_kill_plane(
    library: PromptLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt_library, "KILL_PLANE_Y", -12.25)

    assert library.render_contract() == library.render(
        PromptName.CONTRACT, state_schema=STATE_SCHEMA, kill_plane_y="-12.25"
    )


def test_render_style_guide_is_the_template_style_guide(library: PromptLibrary) -> None:
    assert library.render_style_guide() == library.load(PromptName.STYLE_GUIDE)
