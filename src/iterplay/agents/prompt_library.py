"""Prompts for the Builder, Critic and Fixer, stored as Markdown package data in
``agents/prompts/``.

Prompts that depend on the game template (:data:`TEMPLATE_PROMPTS`) live in
``agents/prompts/<template id>/``; the rest are shared. Prompts use ``string.Template``
placeholders (``$name``), so the JavaScript and JSON that fill them need no brace escaping.
The game contract closes every Builder and Fixer prompt (HLD §12).
"""

from enum import StrEnum
from typing import Final

from iterplay.contract.templates import GameTemplate


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
        raise NotImplementedError

    def render(self, name: PromptName, /, **values: str) -> str:
        """Fill a prompt's placeholders.

        Raises:
            KeyError: A placeholder has no value; prompts are rendered strictly.
        """
        raise NotImplementedError
