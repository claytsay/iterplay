"""``ManagedAgents`` critic backend (HLD §5, stretch goal)."""

from iterplay.agents.critic.base import CriticBackend, CriticInput
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.models.critique import Critique
from iterplay.settings import Settings


class ManagedAgents(CriticBackend):
    """Persona critics and the Lead Designer run as Gemini Managed Agents.

    The Managed Agents API is still to be confirmed from the DeepMind resources doc. The core
    loop must not depend on this class: :class:`~iterplay.agents.critic.direct.DirectCalls`
    is always the fallback.
    """

    def __init__(self, settings: Settings, prompts: PromptLibrary) -> None:
        self._settings = settings
        self._prompts = prompts

    async def critique(self, request: CriticInput) -> Critique:
        raise NotImplementedError
