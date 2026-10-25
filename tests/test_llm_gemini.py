"""The Gemini adapter speaks the google-genai SDK correctly, without touching the network.

A hand-written fake stands in for the SDK's async client (``genai.Client(...).aio``); the
request and response objects are the SDK's own types.
"""

import asyncio
import os
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, SecretStr

from iterplay.errors import LLMError
from iterplay.llm import gemini
from iterplay.llm.base import LLMClient, LLMRequest, MediaInput, parse_response
from iterplay.llm.gemini import GeminiClient
from iterplay.models.critique import Critique
from iterplay.models.run import TokenUsage

FAKE_KEY = "test-key-0123456789abcdef"
MODEL = "gemini-test"
CLIP_NAME = "combined_360p.mp4"


class Answer(BaseModel):
    value: int


@dataclass(frozen=True)
class GenerateCall:
    model: str
    contents: list[types.Content]
    config: types.GenerateContentConfig


class FakeModels:
    """``aio.models``: answers ``generate_content`` from a script of replies and errors."""

    def __init__(self, outcomes: Sequence[types.GenerateContentResponse | Exception]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[GenerateCall] = []

    async def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append(GenerateCall(model, contents, config))
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeFiles:
    """``aio.files``: every upload steps through ``states``, one per call; the last sticks."""

    def __init__(
        self,
        states: Sequence[types.FileState] = (types.FileState.ACTIVE,),
        *,
        upload_errors: Sequence[Exception] = (),
        get_errors: Sequence[Exception] = (),
        uri: bool = True,
    ) -> None:
        self._states = tuple(states)
        self._upload_errors = deque(upload_errors)
        self._get_errors = deque(get_errors)
        self._uri = uri
        self._steps: dict[str, int] = {}
        self.uploads: list[tuple[Path, types.UploadFileConfig]] = []
        self.gets: list[str] = []

    async def upload(self, *, file: Path, config: types.UploadFileConfig) -> types.File:
        await asyncio.sleep(0)
        if self._upload_errors:
            raise self._upload_errors.popleft()
        self.uploads.append((file, config))
        name = f"files/upload-{len(self.uploads)}"
        self._steps[name] = 0
        return self._file(name)

    async def get(self, *, name: str) -> types.File:
        self.gets.append(name)
        if self._get_errors:
            raise self._get_errors.popleft()
        self._steps[name] += 1
        return self._file(name)

    def _file(self, name: str) -> types.File:
        state = self._states[min(self._steps[name], len(self._states) - 1)]
        failed = state == types.FileState.FAILED
        return types.File(
            name=name,
            uri=f"https://files.test/{name}" if self._uri else None,
            state=state,
            error=types.FileStatus(code=3, message="corrupt video") if failed else None,
        )


@dataclass
class FakeGenAI:
    models: FakeModels
    files: FakeFiles = field(default_factory=FakeFiles)


class SleepRecorder:
    """Stands in for ``asyncio.sleep``: records each delay and only yields to the loop."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _reply(
    text: str | None = '{"value": 1}',
    *,
    prompt_tokens: int | None = 120,
    output_tokens: int | None = 30,
    thoughts: int | None = None,
    finish_reason: types.FinishReason | None = types.FinishReason.STOP,
) -> types.GenerateContentResponse:
    parts = [] if text is None else [types.Part(text=text)]
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts), finish_reason=finish_reason
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=thoughts,
        ),
    )


def _api_error(
    code: int, status: str, message: str, details: list[dict[str, Any]] | None = None
) -> genai_errors.APIError:
    body: dict[str, Any] = {"code": code, "status": status, "message": message}
    if details is not None:
        body["details"] = details
    error_type = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return error_type(code, {"error": body})


def _retry_info(delay: str) -> list[dict[str, Any]]:
    return [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]


def _request(*media: MediaInput, **options: Any) -> LLMRequest:
    return LLMRequest(
        purpose=options.pop("purpose", "critic"),
        system_instruction=options.pop("system_instruction", "You are a game critic."),
        prompt=options.pop("prompt", "Critique this run."),
        response_schema=options.pop("response_schema", Answer),
        media=media,
        **options,
    )


def _client(fake: FakeGenAI, sleeper: SleepRecorder, **options: Any) -> GeminiClient:
    return GeminiClient(SecretStr(FAKE_KEY), MODEL, client=fake, sleep=sleeper, **options)


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def clip(tmp_path: Path) -> MediaInput:
    path = tmp_path / CLIP_NAME
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 64)
    return MediaInput(path=path, mime_type="video/mp4")


async def test_requests_json_constrained_by_the_schema(sleeper: SleepRecorder) -> None:
    fake = FakeGenAI(FakeModels([_reply()]))

    response = await _client(fake, sleeper).generate(
        _request(temperature=0.2, max_output_tokens=4096)
    )

    [call] = fake.models.calls
    assert call.model == MODEL
    assert call.contents == [
        types.Content(role="user", parts=[types.Part.from_text(text="Critique this run.")])
    ]
    assert call.config.system_instruction == "You are a game critic."
    assert call.config.response_mime_type == "application/json"
    assert call.config.response_json_schema == Answer.model_json_schema()
    assert call.config.response_schema is None
    assert call.config.temperature == 0.2
    assert call.config.max_output_tokens == 4096
    assert response.text == '{"value": 1}'
    assert response.usage == TokenUsage(input_tokens=120, output_tokens=30)
    assert not response.from_cache
    assert parse_response(response, Answer) == Answer(value=1)


async def test_unset_options_are_left_to_the_api(sleeper: SleepRecorder) -> None:
    fake = FakeGenAI(FakeModels([_reply()]))

    await _client(fake, sleeper).generate(_request(system_instruction=""))

    config = fake.models.calls[0].config
    assert (config.system_instruction, config.temperature, config.max_output_tokens) == (
        None,
        None,
        None,
    )


async def test_dict_fields_survive_in_the_json_schema(
    sleeper: SleepRecorder, hld_critique: dict[str, Any]
) -> None:
    fake = FakeGenAI(FakeModels([_reply(Critique.model_validate(hld_critique).model_dump_json())]))

    response = await _client(fake, sleeper).generate(_request(response_schema=Critique))

    schema = fake.models.calls[0].config.response_json_schema
    assert schema == Critique.model_json_schema()
    assert schema["properties"]["rubric_assessment"]["type"] == "object"
    assert parse_response(response, Critique).rubric_assessment == {"goal_clarity": 2, "feel": 3}


async def test_older_sdks_get_the_pydantic_class(
    sleeper: SleepRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini, "_SDK_ACCEPTS_JSON_SCHEMA", False)
    fake = FakeGenAI(FakeModels([_reply()]))

    await _client(fake, sleeper).generate(_request())

    config = fake.models.calls[0].config
    assert config.response_schema is Answer
    assert config.response_json_schema is None


@pytest.mark.parametrize(
    ("reply", "usage"),
    [
        (_reply(prompt_tokens=None), TokenUsage(input_tokens=0, output_tokens=30)),
        (_reply(output_tokens=None), TokenUsage(input_tokens=120, output_tokens=0)),
        (_reply(thoughts=200), TokenUsage(input_tokens=120, output_tokens=230)),
        (
            types.GenerateContentResponse(candidates=_reply().candidates),
            TokenUsage(input_tokens=0, output_tokens=0),
        ),
    ],
    ids=["no-prompt-count", "no-output-count", "thinking", "no-metadata"],
)
async def test_usage_metadata_maps_to_token_usage(
    sleeper: SleepRecorder, reply: types.GenerateContentResponse, usage: TokenUsage
) -> None:
    response = await _client(FakeGenAI(FakeModels([reply])), sleeper).generate(_request())

    assert response.usage == usage


async def test_media_is_uploaded_polled_and_sent_before_the_prompt(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    processing, active = types.FileState.PROCESSING, types.FileState.ACTIVE
    fake = FakeGenAI(FakeModels([_reply()]), FakeFiles([processing, processing, active]))

    await _client(fake, sleeper).generate(_request(clip))

    assert fake.files.uploads == [
        (clip.path, types.UploadFileConfig(mime_type="video/mp4", display_name=CLIP_NAME))
    ]
    assert fake.files.gets == ["files/upload-1", "files/upload-1"]
    assert sleeper.delays == [gemini.DEFAULT_FILE_POLL_INTERVAL_S] * 2
    assert fake.models.calls[0].contents[0].parts == [
        types.Part.from_uri(file_uri="https://files.test/files/upload-1", mime_type="video/mp4"),
        types.Part.from_text(text="Critique this run."),
    ]


async def test_several_media_keep_their_order(
    tmp_path: Path, sleeper: SleepRecorder, clip: MediaInput
) -> None:
    still = tmp_path / "frame.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    fake = FakeGenAI(FakeModels([_reply()]))

    await _client(fake, sleeper).generate(_request(clip, MediaInput(still, "image/png")))

    parts = fake.models.calls[0].contents[0].parts or []
    files = [
        (part.file_data.file_uri, part.file_data.mime_type) for part in parts if part.file_data
    ]
    assert files == [
        ("https://files.test/files/upload-1", "video/mp4"),
        ("https://files.test/files/upload-2", "image/png"),
    ]
    assert parts[-1].text == "Critique this run."


async def test_uploads_are_reused_within_a_client(sleeper: SleepRecorder, clip: MediaInput) -> None:
    fake = FakeGenAI(FakeModels([_reply(), _reply()]))
    client = _client(fake, sleeper)

    await client.generate(_request(clip))
    await client.generate(_request(clip, prompt="Critique it again."))

    assert len(fake.files.uploads) == 1
    first, second = (call.contents[0].parts for call in fake.models.calls)
    assert first is not None and second is not None
    assert first[0] == second[0]


async def test_a_new_client_uploads_again(sleeper: SleepRecorder, clip: MediaInput) -> None:
    fake = FakeGenAI(FakeModels([_reply(), _reply()]))

    await _client(fake, sleeper).generate(_request(clip))
    await _client(fake, sleeper).generate(_request(clip))

    assert len(fake.files.uploads) == 2


async def test_changed_media_is_uploaded_again(sleeper: SleepRecorder, clip: MediaInput) -> None:
    fake = FakeGenAI(FakeModels([_reply(), _reply(), _reply()]))
    client = _client(fake, sleeper)
    await client.generate(_request(clip))

    clip.path.write_bytes(clip.path.read_bytes() + b"more frames")
    await client.generate(_request(clip))
    stat = clip.path.stat()
    os.utime(clip.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    await client.generate(_request(clip))

    assert len(fake.files.uploads) == 3


async def test_concurrent_requests_share_one_upload(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    states = [types.FileState.PROCESSING, types.FileState.ACTIVE]
    fake = FakeGenAI(FakeModels([_reply(), _reply()]), FakeFiles(states))
    client = _client(fake, sleeper)

    await asyncio.gather(
        client.generate(_request(clip, purpose="critic.random")),
        client.generate(_request(clip, purpose="critic.newbie")),
    )

    assert len(fake.files.uploads) == 1
    assert len(fake.models.calls) == 2


async def test_failed_processing_is_an_error_and_is_not_reused(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    states = [types.FileState.PROCESSING, types.FileState.FAILED]
    fake = FakeGenAI(FakeModels([]), FakeFiles(states))
    client = _client(fake, sleeper)

    with pytest.raises(LLMError, match=f"could not process {CLIP_NAME}: corrupt video"):
        await client.generate(_request(clip))
    with pytest.raises(LLMError):
        await client.generate(_request(clip))

    assert fake.models.calls == []
    assert len(fake.files.uploads) == 2


async def test_processing_is_given_up_after_the_timeout(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    fake = FakeGenAI(FakeModels([]), FakeFiles([types.FileState.PROCESSING]))
    client = _client(fake, sleeper, file_poll_interval_s=1.5, file_ready_timeout_s=4.0)

    with pytest.raises(LLMError, match=f"{CLIP_NAME} was still PROCESSING after 4.5 s"):
        await client.generate(_request(clip))

    assert sleeper.delays == [1.5, 1.5, 1.5]
    assert len(fake.files.gets) == 3
    assert fake.models.calls == []


async def test_an_active_file_without_a_uri_is_an_error(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    fake = FakeGenAI(FakeModels([]), FakeFiles(uri=False))

    with pytest.raises(LLMError, match=f"no URI for {CLIP_NAME}"):
        await _client(fake, sleeper).generate(_request(clip))


async def test_missing_media_is_an_error(tmp_path: Path, sleeper: SleepRecorder) -> None:
    fake = FakeGenAI(FakeModels([]))
    missing = MediaInput(tmp_path / "missing.mp4", "video/mp4")

    with pytest.raises(LLMError, match="Cannot read media file"):
        await _client(fake, sleeper).generate(_request(missing))

    assert fake.files.uploads == []


@pytest.mark.parametrize(
    "error",
    [
        _api_error(503, "UNAVAILABLE", "The model is overloaded."),
        _api_error(500, "INTERNAL", "Internal error."),
        _api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded."),
        _api_error(408, "DEADLINE_EXCEEDED", "Request timed out."),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("server disconnected"),
        TimeoutError(),
        ConnectionResetError("reset by peer"),
    ],
    ids=lambda error: str(getattr(error, "code", None) or type(error).__name__),
)
async def test_transient_failures_are_retried_with_exponential_backoff(
    sleeper: SleepRecorder, error: Exception
) -> None:
    fake = FakeGenAI(FakeModels([error, error, _reply()]))

    response = await _client(fake, sleeper).generate(_request())

    assert response.text == '{"value": 1}'
    assert len(fake.models.calls) == 3
    assert sleeper.delays == [2.0, 4.0]


async def test_retries_give_up_after_three_attempts(
    sleeper: SleepRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    errors = [_api_error(503, "UNAVAILABLE", f"Overloaded ({n}).") for n in range(3)]
    fake = FakeGenAI(FakeModels(errors))

    with pytest.raises(LLMError) as caught:
        await _client(fake, sleeper).generate(_request())

    assert str(caught.value) == (
        "Gemini generate_content for 'critic' failed after 3 attempts: "
        "HTTP 503 UNAVAILABLE: Overloaded (2)."
    )
    assert caught.value.__cause__ is errors[2]
    assert len(fake.models.calls) == gemini.MAX_ATTEMPTS
    assert "retrying in 2.0 s (attempt 2 of 3)" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        _api_error(400, "INVALID_ARGUMENT", "Request contains an invalid argument."),
        _api_error(403, "PERMISSION_DENIED", "Permission denied."),
        _api_error(404, "NOT_FOUND", "Model not found."),
        ValueError("unsupported content"),
    ],
    ids=lambda error: str(getattr(error, "code", type(error).__name__)),
)
async def test_permanent_failures_are_not_retried(sleeper: SleepRecorder, error: Exception) -> None:
    fake = FakeGenAI(FakeModels([error]))

    with pytest.raises(LLMError, match=r"^Gemini generate_content for 'critic' failed: ") as caught:
        await _client(fake, sleeper).generate(_request())

    assert caught.value.__cause__ is error
    assert len(fake.models.calls) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("delay", "expected_s"), [("31s", 31.0), ("0.5s", 2.0), ("3600s", 60.0), ("soon", 2.0)]
)
async def test_rate_limits_wait_as_long_as_the_server_asks(
    sleeper: SleepRecorder, delay: str, expected_s: float
) -> None:
    error = _api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded.", _retry_info(delay))
    fake = FakeGenAI(FakeModels([error, _reply()]))

    await _client(fake, sleeper).generate(_request())

    assert sleeper.delays == [expected_s]


async def test_uploads_and_status_checks_are_retried(
    sleeper: SleepRecorder, clip: MediaInput
) -> None:
    states = [types.FileState.PROCESSING, types.FileState.ACTIVE]
    files = FakeFiles(
        states,
        upload_errors=[_api_error(502, "BAD_GATEWAY", "Bad gateway.")],
        get_errors=[httpx.ReadTimeout("read timed out")],
    )
    fake = FakeGenAI(FakeModels([_reply()]), files)

    await _client(fake, sleeper).generate(_request(clip))

    assert len(files.uploads) == 1
    assert files.gets == ["files/upload-1", "files/upload-1"]
    poll = gemini.DEFAULT_FILE_POLL_INTERVAL_S
    assert sleeper.delays == [2.0, poll, 2.0]


async def test_upload_failures_are_errors(sleeper: SleepRecorder, clip: MediaInput) -> None:
    error = _api_error(400, "INVALID_ARGUMENT", "Unsupported MIME type.")
    fake = FakeGenAI(FakeModels([]), FakeFiles(upload_errors=[error]))

    with pytest.raises(LLMError, match=f"Gemini upload of {CLIP_NAME} failed: HTTP 400"):
        await _client(fake, sleeper).generate(_request(clip))


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (_reply(None, finish_reason=types.FinishReason.SAFETY), " (finish reason: SAFETY)"),
        (_reply(None, finish_reason=types.FinishReason.MAX_TOKENS), " (finish reason: MAX_TOKENS)"),
        (_reply(""), " (finish reason: STOP)"),
        (_reply(" \n ", finish_reason=None), ""),
        (
            types.GenerateContentResponse(
                prompt_feedback=types.GenerateContentResponsePromptFeedback(
                    block_reason=types.BlockedReason.SAFETY
                )
            ),
            " (prompt blocked: SAFETY)",
        ),
    ],
    ids=["safety", "max-tokens", "empty", "whitespace", "blocked"],
)
async def test_an_empty_response_is_an_error(
    sleeper: SleepRecorder, reply: types.GenerateContentResponse, reason: str
) -> None:
    fake = FakeGenAI(FakeModels([reply]))

    with pytest.raises(LLMError) as caught:
        await _client(fake, sleeper).generate(_request())

    assert str(caught.value) == f"Gemini returned no text for 'critic'{reason}"
    assert len(fake.models.calls) == 1


async def test_the_api_key_never_appears_in_errors_or_logs(
    sleeper: SleepRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    echo = f"API key {FAKE_KEY} not valid."
    errors = [_api_error(503, "UNAVAILABLE", echo), _api_error(400, "INVALID_ARGUMENT", echo)]
    fake = FakeGenAI(FakeModels(errors))

    with pytest.raises(LLMError) as caught:
        await _client(fake, sleeper).generate(_request())

    assert FAKE_KEY not in str(caught.value)
    assert "API key [redacted] not valid." in str(caught.value)
    assert caplog.records
    assert FAKE_KEY not in caplog.text


class FakeSDK:
    """Stands in for ``genai.Client``, recording how it was constructed."""

    instances: ClassVar[list["FakeSDK"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.aio = FakeGenAI(FakeModels([_reply(), _reply()]))
        FakeSDK.instances.append(self)


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeSDK]:
    monkeypatch.setattr(FakeSDK, "instances", [])
    monkeypatch.setattr(genai, "Client", FakeSDK)
    return FakeSDK


async def test_the_sdk_client_is_created_once_on_first_use(fake_sdk: type[FakeSDK]) -> None:
    client = GeminiClient(SecretStr(FAKE_KEY), MODEL)
    assert fake_sdk.instances == []

    await client.generate(_request())
    await client.generate(_request())

    [sdk] = fake_sdk.instances
    assert sdk.kwargs == {
        "api_key": FAKE_KEY,
        "vertexai": False,
        "http_options": types.HttpOptions(timeout=900_000),
    }
    assert len(sdk.aio.models.calls) == 2


async def test_the_request_timeout_can_be_disabled(fake_sdk: type[FakeSDK]) -> None:
    await GeminiClient(SecretStr(FAKE_KEY), MODEL, request_timeout_s=None).generate(_request())

    assert fake_sdk.instances[0].kwargs["http_options"] is None


async def test_sdk_client_failures_are_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**kwargs: Any) -> NoReturn:
        raise ValueError(f"Invalid key {kwargs['api_key']}")

    monkeypatch.setattr(genai, "Client", refuse)

    with pytest.raises(LLMError, match=r"Could not create the Gemini client: .*\[redacted\]"):
        await GeminiClient(SecretStr(FAKE_KEY), MODEL).generate(_request())


def test_gemini_client_is_an_llm_client(sleeper: SleepRecorder) -> None:
    client: LLMClient = _client(FakeGenAI(FakeModels([])), sleeper)

    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in repr(vars(client))
