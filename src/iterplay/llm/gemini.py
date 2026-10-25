"""Gemini adapter, built on the ``google-genai`` SDK."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final, Protocol, TypeAlias, TypeVar

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

from iterplay.errors import LLMError
from iterplay.llm.base import LLMRequest, LLMResponse, MediaInput
from iterplay.models.run import TokenUsage

MAX_ATTEMPTS: Final = 3
"""Tries per API call, the first included, before a transient failure becomes an LLMError."""

DEFAULT_REQUEST_TIMEOUT_S: Final = 900.0
"""HTTP timeout for each API request. Generous, because a whole game can take minutes to
decode, but finite, so a connection dropped by a network change fails and is retried."""

DEFAULT_FILE_POLL_INTERVAL_S: Final = 2.0
DEFAULT_FILE_READY_TIMEOUT_S: Final = 300.0
"""Longest wait for an uploaded file to finish processing."""

JSON_MIME_TYPE: Final = "application/json"

_BACKOFF_INITIAL_S: Final = 2.0
_BACKOFF_FACTOR: Final = 2.0
_MAX_BACKOFF_S: Final = 60.0
"""Cap on any one wait, including a delay the server asks for."""

_RETRYABLE_STATUS: Final = frozenset({408, 429})
"""Retryable 4xx statuses; every 5xx is retryable too."""

_TRANSIENT_ERRORS: Final = (
    TimeoutError,
    ConnectionError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
"""Transport failures worth retrying. httpx is the SDK's HTTP transport."""

_SDK_ACCEPTS_JSON_SCHEMA: Final = "response_json_schema" in types.GenerateContentConfig.model_fields
"""Newer SDKs take a plain JSON schema, which handles every pydantic schema iterplay uses;
the older ``response_schema`` may reject some, such as dicts with arbitrary keys."""

_RETRY_DELAY: Final = re.compile(r"(\d+(?:\.\d+)?)s")
_MAX_DETAIL_CHARS: Final = 300

_log = logging.getLogger(__name__)

T = TypeVar("T")

Sleep: TypeAlias = Callable[[float], Awaitable[None]]

_UploadKey: TypeAlias = tuple[Path, int, int]
"""Resolved path, size and modification time (ns) of an uploaded media file."""


class AsyncGenAIModels(Protocol):
    async def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse: ...


class AsyncGenAIFiles(Protocol):
    async def upload(self, *, file: Path, config: types.UploadFileConfig) -> types.File: ...

    async def get(self, *, name: str) -> types.File: ...


class AsyncGenAIClient(Protocol):
    """The part of the SDK's async client, ``genai.Client(...).aio``, that iterplay uses."""

    @property
    def models(self) -> AsyncGenAIModels: ...

    @property
    def files(self) -> AsyncGenAIFiles: ...


class GeminiClient:
    """:class:`~iterplay.llm.base.LLMClient` for the Gemini API, iterplay's only cloud
    dependency (HLD §2).

    Media inputs are uploaded through the Files API and polled until they are ready to use.
    Each file is uploaded once per client, and again only if its size or modification time
    changes. Responses are requested as JSON constrained by the request's response schema.
    Transient failures (HTTP 408, 429 and 5xx, timeouts, dropped connections) are retried
    with exponential backoff, up to :data:`MAX_ATTEMPTS` tries per API call.

    The SDK client is created on first use. Tests pass ``client``, any object with the
    SDK's async surface, and ``sleep``, which replaces :func:`asyncio.sleep` for backoff and
    polling, so they never touch the network or wait.
    """

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        *,
        client: AsyncGenAIClient | None = None,
        sleep: Sleep = asyncio.sleep,
        request_timeout_s: float | None = DEFAULT_REQUEST_TIMEOUT_S,
        file_poll_interval_s: float = DEFAULT_FILE_POLL_INTERVAL_S,
        file_ready_timeout_s: float = DEFAULT_FILE_READY_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._sleep = sleep
        self._request_timeout_s = request_timeout_s
        self._file_poll_interval_s = file_poll_interval_s
        self._file_ready_timeout_s = file_ready_timeout_s
        self._upload_uris: dict[_UploadKey, str] = {}
        self._upload_locks: dict[_UploadKey, asyncio.Lock] = {}

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Upload the request's media, then ask for JSON matching its response schema.

        Media parts precede the prompt text. Usage counts thinking tokens as output, since
        they are billed as output.

        Raises:
            LLMError: An API call failed, a media file could not be read or processed, or the
                response has no text.
        """
        api = self._api()
        parts = [await self._media_part(api, media) for media in request.media]
        parts.append(types.Part.from_text(text=request.prompt))
        contents = [types.Content(role="user", parts=parts)]
        config = _generation_config(request)
        response = await self._call(
            f"generate_content for {request.purpose!r}",
            lambda: api.models.generate_content(
                model=self._model, contents=contents, config=config
            ),
        )
        text = response.text
        if text is None or not text.strip():
            reason = _empty_reason(response)
            raise LLMError(f"Gemini returned no text for {request.purpose!r}{reason}")
        return LLMResponse(text=text, usage=_token_usage(response.usage_metadata))

    def _api(self) -> AsyncGenAIClient:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> AsyncGenAIClient:
        http_options = None
        if self._request_timeout_s is not None:
            http_options = types.HttpOptions(timeout=round(self._request_timeout_s * 1000))
        try:
            sdk = genai.Client(
                api_key=self._api_key.get_secret_value(),
                vertexai=False,
                http_options=http_options,
            )
        except Exception as exc:
            detail = self._redact(_describe(exc))
            raise LLMError(f"Could not create the Gemini client: {detail}") from exc
        return sdk.aio

    async def _media_part(self, api: AsyncGenAIClient, media: MediaInput) -> types.Part:
        return types.Part.from_uri(
            file_uri=await self._uploaded_uri(api, media), mime_type=media.mime_type
        )

    async def _uploaded_uri(self, api: AsyncGenAIClient, media: MediaInput) -> str:
        """URI of the file's active upload, uploading it unless this client already has.

        Concurrent requests for the same file share one upload.
        """
        key = _upload_key(media.path)
        async with self._upload_locks.setdefault(key, asyncio.Lock()):
            if key not in self._upload_uris:
                self._upload_uris[key] = await self._upload(api, media)
            return self._upload_uris[key]

    async def _upload(self, api: AsyncGenAIClient, media: MediaInput) -> str:
        label = media.path.name
        config = types.UploadFileConfig(mime_type=media.mime_type, display_name=label)
        uploaded = await self._call(
            f"upload of {label}", lambda: api.files.upload(file=media.path, config=config)
        )
        _log.debug("Uploaded %s as %s", media.path, uploaded.name)
        return await self._until_active(api, uploaded, label)

    async def _until_active(self, api: AsyncGenAIClient, file: types.File, label: str) -> str:
        """Poll an uploaded file until it is ACTIVE, then return its URI.

        Waits at most ``file_ready_timeout_s`` in total between polls.
        """
        name = file.name
        if not name:
            raise LLMError(f"Gemini returned no file name for the upload of {label}")
        waited_s = 0.0
        while file.state != types.FileState.ACTIVE:
            if file.state == types.FileState.FAILED:
                raise LLMError(f"Gemini could not process {label}{_file_error(file)}")
            if waited_s >= self._file_ready_timeout_s:
                raise LLMError(
                    f"{label} was still {_state_name(file)} after {waited_s:g} s of processing"
                )
            await self._sleep(self._file_poll_interval_s)
            waited_s += self._file_poll_interval_s
            file = await self._call(f"status check of {label}", lambda: api.files.get(name=name))
        if not file.uri:
            raise LLMError(f"Gemini returned no URI for {label}")
        return file.uri

    async def _call(self, action: str, call: Callable[[], Awaitable[T]]) -> T:
        """Run one API call, retrying transient failures with exponential backoff."""
        attempt = 1
        while True:
            try:
                return await call()
            except Exception as exc:
                detail = self._redact(_describe(exc))
                if attempt >= MAX_ATTEMPTS or not _is_transient(exc):
                    tries = f" after {attempt} attempts" if attempt > 1 else ""
                    raise LLMError(f"Gemini {action} failed{tries}: {detail}") from exc
                delay_s = _backoff_s(exc, attempt)
                _log.warning(
                    "Gemini %s failed (%s); retrying in %.1f s (attempt %d of %d)",
                    action,
                    detail,
                    delay_s,
                    attempt + 1,
                    MAX_ATTEMPTS,
                )
                await self._sleep(delay_s)
                attempt += 1

    def _redact(self, text: str) -> str:
        """``text`` without the API key, as defence in depth for messages and logs."""
        key = self._api_key.get_secret_value()
        return text.replace(key, "[redacted]") if key else text


def _generation_config(request: LLMRequest) -> types.GenerateContentConfig:
    schema: dict[str, Any] = (
        {"response_json_schema": request.response_schema.model_json_schema()}
        if _SDK_ACCEPTS_JSON_SCHEMA
        else {"response_schema": request.response_schema}
    )
    return types.GenerateContentConfig(
        system_instruction=request.system_instruction or None,
        response_mime_type=JSON_MIME_TYPE,
        temperature=request.temperature,
        max_output_tokens=request.max_output_tokens,
        **schema,
    )


def _upload_key(path: Path) -> _UploadKey:
    try:
        stat = path.stat()
    except OSError as exc:
        raise LLMError(f"Cannot read media file {path}: {exc.strerror or exc}") from exc
    return path.resolve(), stat.st_size, stat.st_mtime_ns


def _token_usage(metadata: types.GenerateContentResponseUsageMetadata | None) -> TokenUsage:
    if metadata is None:
        return TokenUsage()
    thoughts = getattr(metadata, "thoughts_token_count", None) or 0
    return TokenUsage(
        input_tokens=metadata.prompt_token_count or 0,
        output_tokens=(metadata.candidates_token_count or 0) + thoughts,
    )


def _is_transient(error: Exception) -> bool:
    if isinstance(error, genai_errors.APIError):
        code = error.code
        return isinstance(code, int) and (code in _RETRYABLE_STATUS or 500 <= code < 600)
    return isinstance(error, _TRANSIENT_ERRORS)


def _backoff_s(error: Exception, attempt: int) -> float:
    """Exponential backoff, or the server's requested delay when that is longer."""
    delay_s = _BACKOFF_INITIAL_S * _BACKOFF_FACTOR ** (attempt - 1)
    requested_s = _requested_delay_s(error) or 0.0
    return min(max(delay_s, requested_s), _MAX_BACKOFF_S)


def _requested_delay_s(error: Exception) -> float | None:
    """The ``google.rpc.RetryInfo`` delay in an API error (sent with 429s), if any."""
    if not isinstance(error, genai_errors.APIError) or not isinstance(error.details, dict):
        return None
    body = error.details.get("error", error.details)
    entries = body.get("details") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("@type", "")).endswith("rpc.RetryInfo"):
            delay = _RETRY_DELAY.fullmatch(str(entry.get("retryDelay", "")))
            return float(delay[1]) if delay is not None else None
    return None


def _describe(error: BaseException) -> str:
    """A one-line, bounded description of ``error`` for messages and logs."""
    if isinstance(error, genai_errors.APIError):
        status = " ".join(str(part) for part in (error.code, error.status) if part)
        detail = f"HTTP {status}: {error.message or error.details}"
    elif str(error):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = type(error).__name__
    detail = " ".join(detail.split())
    return detail if len(detail) <= _MAX_DETAIL_CHARS else detail[: _MAX_DETAIL_CHARS - 3] + "..."


def _empty_reason(response: types.GenerateContentResponse) -> str:
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        return f" (prompt blocked: {feedback.block_reason.value})"
    if response.candidates and response.candidates[0].finish_reason is not None:
        return f" (finish reason: {response.candidates[0].finish_reason.value})"
    return ""


def _file_error(file: types.File) -> str:
    return f": {file.error.message}" if file.error is not None and file.error.message else ""


def _state_name(file: types.File) -> str:
    return file.state.value if file.state is not None else "unprocessed"
