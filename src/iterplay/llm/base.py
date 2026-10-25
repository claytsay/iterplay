"""Provider-agnostic interface for the model calls made by the Builder, Critic and Fixer."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from iterplay.models.run import TokenUsage

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class MediaInput:
    """A local media file to attach to a request.

    Clients upload it (Gemini Files API) only on a cache miss; cache keys hash its bytes.
    """

    path: Path
    mime_type: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    purpose: str
    """Short label such as ``builder`` or ``critic.speedrunner``; names cache entries."""

    system_instruction: str
    prompt: str

    response_schema: type[BaseModel]
    """The model must answer with JSON matching this schema."""

    media: tuple[MediaInput, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    """Raw JSON text; validate it with :func:`parse_response`."""

    usage: TokenUsage
    from_cache: bool = False


class LLMClient(Protocol):
    """Anything that can answer an :class:`LLMRequest`."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Run one request.

        Raises:
            LLMError: The call failed.
            BudgetExceededError: The run's budget is spent (metered clients only).
        """
        ...


def parse_response(response: LLMResponse, schema: type[ModelT]) -> ModelT:
    """Validate a response's JSON against ``schema``.

    Raises:
        LLMResponseError: The text is not valid JSON for ``schema``.
    """
    raise NotImplementedError
