"""Content-addressed response cache (HLD §6).

Keys hash the model, system instruction, prompt, response schema and the bytes of every media
file, so re-running an identical step costs nothing. Entries keep the full prompt next to the
response, which makes each version's ``llm/`` a record of every model exchange.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from iterplay.llm.base import LLMClient, LLMRequest, LLMResponse
from iterplay.models.base import StrictModel
from iterplay.models.run import TokenUsage

_KEY: Final = re.compile(r"[0-9a-f]{64}")

_log = logging.getLogger(__name__)


class _CachedMedia(StrictModel):
    path: str
    mime_type: str
    sha256: str


class _CachedRequest(StrictModel):
    purpose: str
    model: str
    system_instruction: str
    prompt: str
    response_schema: str
    """Name of the response schema class."""

    media: list[_CachedMedia]
    temperature: float | None
    max_output_tokens: int | None


class _CachedResponse(StrictModel):
    text: str
    usage: TokenUsage


class _CacheEntry(StrictModel):
    """Contents of ``llm/<key>.json``."""

    key: str
    request: _CachedRequest
    response: _CachedResponse


class ResponseCache:
    """One JSON file per request, ``<key>.json``, in ``directory`` (a version's ``llm/``)."""

    def __init__(self, directory: Path, model: str) -> None:
        self._directory = directory
        self._model = model

    def key(self, request: LLMRequest) -> str:
        """SHA-256 hex digest identifying ``request``.

        It covers the model, system instruction, prompt, the response schema's JSON schema,
        temperature, output token limit, and each media file's MIME type and content digest,
        in order. The purpose is only a label, and media paths do not matter.

        Raises:
            OSError: A media file cannot be read.
        """
        identity = {
            "model": self._model,
            "system_instruction": request.system_instruction,
            "prompt": request.prompt,
            "response_schema": request.response_schema.model_json_schema(),
            "temperature": None if request.temperature is None else float(request.temperature),
            "max_output_tokens": request.max_output_tokens,
            "media": [
                {"mime_type": media.mime_type, "sha256": _file_sha256(media.path)}
                for media in request.media
            ],
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def get(self, key: str) -> LLMResponse | None:
        """The cached response, marked ``from_cache``, or None on a miss.

        An unreadable or corrupt entry counts as a miss; the next :meth:`put` replaces it.
        """
        path = self._entry_path(key)
        try:
            entry = _CacheEntry.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            _log.warning("Ignoring unreadable cache entry %s: %s", path, _first_line(exc))
            return None
        if entry.key != key:
            _log.warning("Ignoring cache entry %s, which belongs to another key", path)
            return None
        response = entry.response
        return LLMResponse(text=response.text, usage=response.usage, from_cache=True)

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        """Store the request (prompt text and media digests) and its response.

        The entry is written atomically, replacing any existing one.

        Raises:
            OSError: A media file cannot be read, or the entry cannot be written.
        """
        entry = _CacheEntry(
            key=key,
            request=_CachedRequest(
                purpose=request.purpose,
                model=self._model,
                system_instruction=request.system_instruction,
                prompt=request.prompt,
                response_schema=request.response_schema.__name__,
                media=[
                    _CachedMedia(
                        path=media.path.as_posix(),
                        mime_type=media.mime_type,
                        sha256=_file_sha256(media.path),
                    )
                    for media in request.media
                ],
                temperature=request.temperature,
                max_output_tokens=request.max_output_tokens,
            ),
            response=_CachedResponse(text=response.text, usage=response.usage),
        )
        _write_atomic(self._entry_path(key), entry.model_dump_json(indent=2) + "\n")

    def _entry_path(self, key: str) -> Path:
        if _KEY.fullmatch(key) is None:
            raise ValueError(f"Not a cache key: {key!r}")
        return self._directory / f"{key}.json"


class CachingLLMClient:
    """:class:`~iterplay.llm.base.LLMClient` decorator that answers repeats from the cache."""

    def __init__(self, inner: LLMClient, cache: ResponseCache) -> None:
        self._inner = inner
        self._cache = cache

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Answer from the cache, or call ``inner`` and store its response.

        A response that cannot be stored is still returned; the failure is logged.
        """
        key = self._cache.key(request)
        cached = self._cache.get(key)
        if cached is not None:
            _log.debug("Cache hit for %s (%s)", request.purpose, key)
            return cached
        response = await self._inner.generate(request)
        try:
            self._cache.put(key, request, response)
        except OSError as exc:
            _log.warning("Could not cache the %s response: %s", request.purpose, exc)
        return replace(response, from_cache=False)


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to a temporary file next to ``path``, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _first_line(error: Exception) -> str:
    return str(error).partition("\n")[0]
