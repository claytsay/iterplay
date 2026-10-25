"""Content-addressed response cache (HLD §6).

Keys hash the model, system instruction, prompt, response schema and the bytes of every media
file, so re-running an identical step costs nothing. Entries keep the full prompt next to the
response, which makes each version's ``llm/`` a record of every model exchange.
"""

from pathlib import Path

from iterplay.llm.base import LLMClient, LLMRequest, LLMResponse


class ResponseCache:
    """One JSON file per request in ``directory`` (a version's ``llm/``)."""

    def __init__(self, directory: Path, model: str) -> None:
        self._directory = directory
        self._model = model

    def key(self, request: LLMRequest) -> str:
        """SHA-256 hex digest identifying ``request``."""
        raise NotImplementedError

    def get(self, key: str) -> LLMResponse | None:
        """The cached response, marked ``from_cache``, or None on a miss."""
        raise NotImplementedError

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        """Store the request (prompt text and media digests) and its response."""
        raise NotImplementedError


class CachingLLMClient:
    """:class:`~iterplay.llm.base.LLMClient` decorator that answers repeats from the cache."""

    def __init__(self, inner: LLMClient, cache: ResponseCache) -> None:
        self._inner = inner
        self._cache = cache

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
