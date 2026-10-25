"""[1] Builder: design doc to v1 (HLD §4 [1])."""

from iterplay.agents.guardrails import GuardrailResult
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.llm.base import LLMClient


class Builder:
    """Asks the model for a complete ``game/`` directory, then applies the guardrails.

    The prompt names the kind of game (``prompts.template.description``) and combines the
    design doc with the template's style guide and game contract. The response must match
    ``BuilderResponse``.
    """

    def __init__(self, llm: LLMClient, prompts: PromptLibrary) -> None:
        self._llm = llm
        self._prompts = prompts

    async def build(self, design_doc: str) -> GuardrailResult:
        """Generate v1.

        Raises:
            LLMError: The model call failed or returned invalid JSON.
            GuardrailError: The returned file set is unusable.
        """
        raise NotImplementedError
