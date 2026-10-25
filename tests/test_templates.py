"""Every game template is complete and self-consistent, so adding one (say, 3D) is safe."""

from importlib.resources import files

import pytest

from iterplay.agents.prompt_library import TEMPLATE_PROMPTS
from iterplay.contract.controls import Control
from iterplay.contract.templates import DEFAULT_TEMPLATE, TEMPLATES, GameTemplate


@pytest.fixture(params=list(TEMPLATES.values()), ids=list(TEMPLATES))
def template(request: pytest.FixtureRequest) -> GameTemplate:
    template: GameTemplate = request.param
    return template


def test_default_template_is_registered() -> None:
    assert DEFAULT_TEMPLATE in TEMPLATES


def test_template_is_registered_under_its_id(template: GameTemplate) -> None:
    assert TEMPLATES[template.id] is template


def test_controls_cover_the_harness(template: GameTemplate) -> None:
    controls = template.controls

    assert {Control.JUMP, Control.RESPAWN} <= controls.bindings.keys()
    assert controls.smoke_move in controls.directions
    assert controls.directions.keys() <= controls.bindings.keys()
    assert all(keys for keys in controls.bindings.values())


def test_2d_templates_never_move_in_depth(template: GameTemplate) -> None:
    if template.dimensions != 2:
        pytest.skip("3D template")

    assert {Control.FORWARD, Control.BACK}.isdisjoint(template.controls.bindings)
    assert all(z == 0 for _, _, z in template.controls.directions.values())


def test_template_has_its_own_prompts(template: GameTemplate) -> None:
    prompts = files("iterplay.agents") / "prompts" / template.id

    for name in TEMPLATE_PROMPTS:
        assert (prompts / f"{name}.md").is_file(), f"missing prompts/{template.id}/{name}.md"
