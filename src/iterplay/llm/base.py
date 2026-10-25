"""Provider-agnostic interface for the model calls made by the Builder, Critic and Fixer."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from iterplay.errors import LLMResponseError
from iterplay.models.run import TokenUsage

ModelT = TypeVar("ModelT", bound=BaseModel)

_CODE_FENCE: Final = re.compile(
    r"\s*```[ \t]*(?:json)?[ \t]*\r?\n?(?P<body>.*?)\s*```\s*", re.DOTALL | re.IGNORECASE
)
"""A whole response wrapped in a Markdown code fence, untagged or tagged ``json``; applied
with ``fullmatch``."""

_MAX_REPORTED_ERRORS: Final = 5
_MAX_SUMMARY_CHARS: Final = 800
_MAX_INPUT_CHARS: Final = 60


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

    A Markdown code fence around the whole text (untagged or tagged ``json``) is stripped
    first, since models sometimes add one despite the JSON response type.

    Raises:
        LLMResponseError: The text is not valid JSON for ``schema``. The message is a short
            summary of what was wrong, suitable for feeding back to the model on a retry.
    """
    try:
        return schema.model_validate_json(_strip_code_fence(response.text))
    except ValidationError as exc:
        raise LLMResponseError(_summarize(exc, schema)) from exc


def _strip_code_fence(text: str) -> str:
    fenced = _CODE_FENCE.fullmatch(text)
    return text if fenced is None else fenced["body"]


def _summarize(error: ValidationError, schema: type[BaseModel]) -> str:
    details = error.errors(include_url=False)
    problems = [_describe(detail) for detail in details[:_MAX_REPORTED_ERRORS]]
    if len(details) > _MAX_REPORTED_ERRORS:
        problems.append(f"and {len(details) - _MAX_REPORTED_ERRORS} more")
    return _truncate(f"Response does not match {schema.__name__}: {'; '.join(problems)}")


def _describe(detail: ErrorDetails) -> str:
    problem = detail["msg"]
    value = detail.get("input")
    if detail["type"] != "json_invalid" and isinstance(value, str | int | float):
        problem += f" (got {_truncate(json.dumps(value), _MAX_INPUT_CHARS)})"
    location = _location(detail["loc"])
    return f"{location}: {problem}" if location else problem


def _location(loc: tuple[int | str, ...]) -> str:
    """``("issues", 0, "severity")`` as ``issues[0].severity``."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path


def _truncate(text: str, limit: int = _MAX_SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
