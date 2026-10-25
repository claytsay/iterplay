"""``ManagedAgents`` critic backend (HLD §5, stretch goal)."""

from typing import Final

from iterplay.agents.critic.base import CriticBackend, CriticInput
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.errors import ConfigError
from iterplay.models.critique import Critique
from iterplay.settings import Settings

NOT_IMPLEMENTED: Final = (
    "The managed critic backend is not implemented yet: the Gemini Managed Agents API is not "
    'available. Use "--critic direct" instead.'
)


class ManagedAgents(CriticBackend):
    """Persona critics and the Lead Designer as Gemini Managed Agents: not implemented yet.

    The Managed Agents API is not available, so constructing this backend raises
    ``ConfigError``. Use :class:`~iterplay.agents.critic.direct.DirectCalls` (``--critic
    direct``) instead: its persona mode runs the same persona critics and Lead Designer as
    plain parallel calls. The core loop never depends on this class.

    Raises:
        ConfigError: Always, on construction.
    """

    def __init__(self, settings: Settings, prompts: PromptLibrary) -> None:
        raise ConfigError(NOT_IMPLEMENTED)

    async def critique(self, request: CriticInput) -> Critique:
        raise ConfigError(NOT_IMPLEMENTED)
