"""[1] Builder: design doc to v1 (HLD §4 [1])."""

import logging
from dataclasses import replace
from typing import Final, TypeVar

from iterplay.agents.guardrails import GuardrailResult, enforce_guardrails
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.errors import GuardrailError, LLMResponseError
from iterplay.llm.base import LLMClient, LLMRequest, parse_response
from iterplay.models.game import BuilderResponse, FixerResponse

FilesResponseT = TypeVar("FilesResponseT", BuilderResponse, FixerResponse)
"""A response schema carrying a complete file set."""

_SYSTEM_INSTRUCTION: Final = (
    "You are an expert browser-game developer. Respond only with JSON that matches the "
    "response schema."
)

_log = logging.getLogger(__name__)


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

        A response that is not valid JSON for the schema, or whose files break the
        guardrails, is retried once with the problem appended to the prompt.

        Raises:
            LLMError: The model call failed, or the retry returned invalid JSON.
            GuardrailError: The retry's file set is unusable.
        """
        prompts = self._prompts
        prompt = prompts.render(
            PromptName.BUILDER,
            game_kind=prompts.template.description,
            design_doc=design_doc,
            style_guide=prompts.render_style_guide(),
            contract=prompts.render_contract(),
        )
        _, files = await generate_files(
            self._llm, purpose="builder", prompt=prompt, schema=BuilderResponse
        )
        return files


async def generate_files(
    llm: LLMClient, *, purpose: str, prompt: str, schema: type[FilesResponseT]
) -> tuple[FilesResponseT, GuardrailResult]:
    """Ask for a complete file set and apply the guardrails. Shared by the Builder and Fixer.

    A response that does not match ``schema``, or whose files break the guardrails, is
    retried once with the problem appended to the prompt, so the model can correct it; a
    second failure propagates. Failed model calls are not retried here. Files dropped by the
    allowlist are logged as a warning.

    Raises:
        LLMError: The model call failed, or the retry returned invalid JSON.
        GuardrailError: The retry's file set is unusable.
    """
    request = LLMRequest(
        purpose=purpose,
        system_instruction=_SYSTEM_INSTRUCTION,
        prompt=prompt,
        response_schema=schema,
    )
    try:
        return await _generate_once(llm, request, schema)
    except (LLMResponseError, GuardrailError) as exc:
        _log.warning("Retrying the rejected %s response once: %s", purpose, exc)
        request = replace(request, prompt=prompt + _rejection_note(exc))
    return await _generate_once(llm, request, schema)


async def _generate_once(
    llm: LLMClient, request: LLMRequest, schema: type[FilesResponseT]
) -> tuple[FilesResponseT, GuardrailResult]:
    response = parse_response(await llm.generate(request), schema)
    files = enforce_guardrails(response.files)
    if files.rejected_paths:
        _log.warning(
            "Discarded %s files outside the allowlist: %s",
            request.purpose,
            ", ".join(repr(path) for path in files.rejected_paths),
        )
    return response, files


def _rejection_note(problem: Exception) -> str:
    """What the retry appends to the prompt."""
    return (
        "\n\n## Your previous response was rejected\n"
        f"{problem}\n\n"
        "Respond again with the complete response, fixing every problem above."
    )
