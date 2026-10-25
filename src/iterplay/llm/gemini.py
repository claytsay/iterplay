"""Gemini adapter, built on the ``google-genai`` SDK."""

from pydantic import SecretStr

from iterplay.llm.base import LLMRequest, LLMResponse


class GeminiClient:
    """:class:`~iterplay.llm.base.LLMClient` for the Gemini API, iterplay's only cloud
    dependency (HLD §2).

    Media inputs are uploaded through the Files API and polled until they are ready to use.
    Responses are requested as JSON constrained by the request's response schema.
    """

    def __init__(self, api_key: SecretStr, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
