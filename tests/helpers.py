"""Test doubles shared across the test suite."""

from collections import deque
from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel

from iterplay.llm.base import LLMRequest, LLMResponse
from iterplay.models.run import TokenUsage

Reply = BaseModel | str | Exception
Responder = Callable[[LLMRequest], Reply]


class FakeLLMClient:
    """Scripted ``LLMClient``: answers from per-purpose reply queues, then a responder.

    A request is matched by its exact purpose first, then by the purpose's prefix before the
    first "." (so replies queued under "critic" also serve "critic.random"). Exceptions are
    raised instead of returned. Every request is recorded in ``requests``.
    """

    def __init__(
        self,
        replies: Mapping[str, Sequence[Reply]] | None = None,
        *,
        responder: Responder | None = None,
    ) -> None:
        self._queues = {purpose: deque(items) for purpose, items in (replies or {}).items()}
        self._responder = responder
        self.requests: list[LLMRequest] = []

    def requests_for(self, purpose: str) -> list[LLMRequest]:
        """Recorded requests whose purpose is ``purpose`` or starts with ``purpose + "."``."""
        return [
            r for r in self.requests if r.purpose == purpose or r.purpose.startswith(purpose + ".")
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        reply = self._next_reply(request)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else reply.model_dump_json()
        usage = TokenUsage(
            input_tokens=max(1, len(request.prompt) // 4),
            output_tokens=max(1, len(text) // 4),
        )
        return LLMResponse(text=text, usage=usage)

    def _next_reply(self, request: LLMRequest) -> Reply:
        for key in (request.purpose, request.purpose.split(".", 1)[0]):
            queue = self._queues.get(key)
            if queue:
                return queue.popleft()
        if self._responder is not None:
            return self._responder(request)
        raise AssertionError(f"FakeLLMClient has no reply for purpose {request.purpose!r}")
