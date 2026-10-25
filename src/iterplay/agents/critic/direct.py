"""``DirectCalls`` critic backend: plain model calls, no agent framework (HLD §5)."""

from collections.abc import Mapping

from iterplay.agents.critic.base import CriticBackend, CriticInput
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.llm.base import LLMClient
from iterplay.models.critique import Critique
from iterplay.models.playtest import BotName


class DirectCalls(CriticBackend):
    """The MVP critic.

    Single-pass mode sends the combined clip to one critic (HLD §4 [5]). Persona mode runs one
    critic per bot clip in parallel with ``asyncio.gather``, then a Lead Designer call merges,
    de-duplicates and prioritises their findings, keeping at most ``max_issues`` (HLD §5).
    """

    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptLibrary,
        *,
        per_persona: bool = False,
        max_issues: int = 5,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._per_persona = per_persona
        self._max_issues = max_issues

    async def critique(self, request: CriticInput) -> Critique:
        raise NotImplementedError

    async def _critique_combined(self, request: CriticInput) -> Critique:
        raise NotImplementedError

    async def _critique_persona(self, request: CriticInput, bot: BotName) -> Critique:
        """Critique one bot's clip only, for deeper and more focused findings."""
        raise NotImplementedError

    async def _lead_designer(
        self, request: CriticInput, critiques: Mapping[BotName, Critique]
    ) -> Critique:
        """Merge persona critiques, resolving conflicts and applying the rubric."""
        raise NotImplementedError
