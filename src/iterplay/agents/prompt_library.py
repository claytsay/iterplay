"""Prompts for the Builder, Critic and Fixer, stored as Markdown package data in
``agents/prompts/``.

Prompts that depend on the game template (:data:`TEMPLATE_PROMPTS`) live in
``agents/prompts/<template id>/``; the rest are shared. Prompts use ``string.Template``
placeholders (``$name``), so the JavaScript and JSON that fill them need no brace escaping.
The game contract closes every Builder and Fixer prompt (HLD §12).
"""

import json
from enum import StrEnum
from importlib.resources import files
from importlib.resources.abc import Traversable
from string import Template
from typing import Final

from iterplay.contract.state import KILL_PLANE_Y, GameState
from iterplay.contract.templates import GameTemplate

_PROMPTS_DIR: Final[Traversable] = files("iterplay.agents") / "prompts"


class PromptName(StrEnum):
    BUILDER = "builder"
    CRITIC = "critic"
    PERSONA_CRITIC = "persona_critic"
    LEAD_DESIGNER = "lead_designer"
    FIXER = "fixer"
    CONTRACT = "contract"
    STYLE_GUIDE = "style_guide"


TEMPLATE_PROMPTS: Final = frozenset({PromptName.CONTRACT, PromptName.STYLE_GUIDE})
"""Prompts with one version per game template."""


class PromptLibrary:
    """Loads and renders the prompts for one game template."""

    def __init__(self, template: GameTemplate) -> None:
        self.template = template

    def load(self, name: PromptName) -> str:
        """Raw text of ``prompts/<name>.md``, or ``prompts/<template id>/<name>.md`` for
        :data:`TEMPLATE_PROMPTS`."""
        directory = _PROMPTS_DIR / self.template.id if name in TEMPLATE_PROMPTS else _PROMPTS_DIR
        return (directory / f"{name}.md").read_text(encoding="utf-8")

    def render(self, name: PromptName, /, **values: str) -> str:
        """Fill a prompt's placeholders.

        Values are inserted verbatim: a ``$`` inside one is never treated as a placeholder.

        Raises:
            KeyError: A placeholder has no value; prompts are rendered strictly.
        """
        return Template(self.load(name)).substitute(values)

    def render_contract(self) -> str:
        """The template's game contract, with the ``getState()`` JSON schema and the kill plane
        filled in. The Builder and Fixer pass it as their prompts' ``contract``."""
        return self.render(
            PromptName.CONTRACT,
            state_schema=json.dumps(GameState.model_json_schema(by_alias=True), indent=2),
            kill_plane_y=_compact_number(KILL_PLANE_Y),
        )

    def render_style_guide(self) -> str:
        """The template's style guide. The Builder and Fixer pass it as ``style_guide``."""
        return self.render(PromptName.STYLE_GUIDE)


def _compact_number(value: float) -> str:
    """``-20.0`` as ``-20``; any other value keeps every digit, e.g. ``-20.5``."""
    return str(int(value)) if value.is_integer() else repr(value)
