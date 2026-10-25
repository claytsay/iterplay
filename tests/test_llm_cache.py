"""The response cache is content-addressed and durable, so identical steps cost nothing (HLD §6)."""

import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from iterplay.errors import LLMError
from iterplay.llm.base import LLMRequest, LLMResponse, MediaInput
from iterplay.llm.cache import CachingLLMClient, ResponseCache
from iterplay.models.run import TokenUsage
from tests.helpers import FakeLLMClient

MODEL = "gemini-test"


class Answer(BaseModel):
    value: int


class OtherAnswer(BaseModel):
    value: str


@pytest.fixture
def clip(tmp_path: Path) -> MediaInput:
    path = tmp_path / "media" / "combined_360p.mp4"
    path.parent.mkdir()
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 1000)
    return MediaInput(path=path, mime_type="video/mp4")


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "v1" / "llm", MODEL)


def _request(**changes: Any) -> LLMRequest:
    request = LLMRequest(
        purpose="critic",
        system_instruction="You are a game critic.",
        prompt="Critique this run.",
        response_schema=Answer,
    )
    return replace(request, **changes)


def _response(text: str = '{"value": 1}') -> LLMResponse:
    return LLMResponse(text=text, usage=TokenUsage(input_tokens=120, output_tokens=30))


def _entries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_key_is_a_stable_sha256_digest(tmp_path: Path, clip: MediaInput) -> None:
    request = _request(media=(clip,), temperature=0.4, max_output_tokens=2048)

    key = ResponseCache(tmp_path / "a", MODEL).key(request)

    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert ResponseCache(tmp_path / "b", MODEL).key(request) == key


def test_key_ignores_the_purpose(cache: ResponseCache) -> None:
    assert cache.key(_request(purpose="critic.random")) == cache.key(_request(purpose="fixer"))


@pytest.mark.parametrize(
    "changes",
    [
        {"system_instruction": "You are a lenient critic."},
        {"prompt": "Critique this run harshly."},
        {"response_schema": OtherAnswer},
        {"temperature": 0.7},
        {"max_output_tokens": 1024},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_key_covers_every_request_input(cache: ResponseCache, changes: dict[str, Any]) -> None:
    assert cache.key(_request(**changes)) != cache.key(_request())


def test_key_covers_the_model(tmp_path: Path) -> None:
    key_a = ResponseCache(tmp_path, "model-a").key(_request())
    key_b = ResponseCache(tmp_path, "model-b").key(_request())

    assert key_a != key_b


def test_key_treats_integer_temperatures_like_floats(cache: ResponseCache) -> None:
    assert cache.key(_request(temperature=1)) == cache.key(_request(temperature=1.0))


def test_key_hashes_media_bytes_not_paths(
    tmp_path: Path, cache: ResponseCache, clip: MediaInput
) -> None:
    copy = tmp_path / "elsewhere.mp4"
    copy.write_bytes(clip.path.read_bytes())
    edited = tmp_path / "edited.mp4"
    edited.write_bytes(clip.path.read_bytes() + b"\x00")

    key = cache.key(_request(media=(clip,)))

    assert cache.key(_request(media=(MediaInput(copy, "video/mp4"),))) == key
    assert cache.key(_request(media=(MediaInput(edited, "video/mp4"),))) != key
    assert cache.key(_request(media=(MediaInput(clip.path, "video/webm"),))) != key
    assert cache.key(_request()) != key


def test_key_depends_on_media_order(tmp_path: Path, cache: ResponseCache, clip: MediaInput) -> None:
    other = tmp_path / "speedrunner_360p.mp4"
    other.write_bytes(b"another clip")
    second = MediaInput(other, "video/mp4")

    assert cache.key(_request(media=(clip, second))) != cache.key(_request(media=(second, clip)))


def test_key_fails_for_missing_media(cache: ResponseCache, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cache.key(_request(media=(MediaInput(tmp_path / "missing.mp4", "video/mp4"),)))


def test_get_misses_on_an_empty_cache(cache: ResponseCache) -> None:
    assert cache.get(cache.key(_request())) is None


def test_put_then_get_marks_the_response_cached(cache: ResponseCache) -> None:
    key = cache.key(_request())

    cache.put(key, _request(), _response())

    assert cache.get(key) == replace(_response(), from_cache=True)


def test_entry_records_the_whole_exchange(
    tmp_path: Path, cache: ResponseCache, clip: MediaInput
) -> None:
    request = _request(purpose="critic.random", media=(clip,), temperature=0.2)
    key = cache.key(request)

    cache.put(key, request, _response())

    entry = json.loads((tmp_path / "v1" / "llm" / f"{key}.json").read_text(encoding="utf-8"))
    assert entry == {
        "key": key,
        "request": {
            "purpose": "critic.random",
            "model": MODEL,
            "system_instruction": "You are a game critic.",
            "prompt": "Critique this run.",
            "response_schema": "Answer",
            "media": [
                {
                    "path": clip.path.as_posix(),
                    "mime_type": "video/mp4",
                    "sha256": hashlib.sha256(clip.path.read_bytes()).hexdigest(),
                }
            ],
            "temperature": 0.2,
            "max_output_tokens": None,
        },
        "response": {
            "text": '{"value": 1}',
            "usage": {"input_tokens": 120, "output_tokens": 30},
        },
    }


def test_put_creates_the_directory_and_leaves_no_temporary_files(
    tmp_path: Path, cache: ResponseCache
) -> None:
    key = cache.key(_request())

    cache.put(key, _request(), _response())
    cache.put(key, _request(), _response('{"value": 2}'))

    assert _entries(tmp_path / "v1" / "llm") == [f"{key}.json"]
    assert cache.get(key) == replace(_response('{"value": 2}'), from_cache=True)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b'{"key": "abc", "request": ',
        b"\xff\xfe\x00",
        b"[]",
        b'{"key": "abc"}',
        json.dumps({"response": {"text": "{}", "usage": {}}}).encode(),
    ],
    ids=["empty", "truncated", "binary", "array", "partial", "no-request"],
)
def test_corrupt_entries_are_misses_until_overwritten(
    tmp_path: Path, cache: ResponseCache, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    key = cache.key(_request())
    directory = tmp_path / "v1" / "llm"
    directory.mkdir(parents=True)
    (directory / f"{key}.json").write_bytes(content)

    assert cache.get(key) is None
    assert "unreadable cache entry" in caplog.text

    cache.put(key, _request(), _response())
    assert cache.get(key) == replace(_response(), from_cache=True)


def test_an_entry_filed_under_another_key_is_a_miss(tmp_path: Path, cache: ResponseCache) -> None:
    key = cache.key(_request())
    other_key = cache.key(_request(prompt="Something else."))
    cache.put(other_key, _request(prompt="Something else."), _response())
    directory = tmp_path / "v1" / "llm"
    (directory / f"{other_key}.json").rename(directory / f"{key}.json")

    assert cache.get(key) is None


@pytest.mark.parametrize("key", ["", "../../outside", "A" * 64, "0" * 63, "0" * 64 + ".json"])
def test_malformed_keys_are_rejected(cache: ResponseCache, key: str) -> None:
    with pytest.raises(ValueError, match="Not a cache key"):
        cache.get(key)
    with pytest.raises(ValueError, match="Not a cache key"):
        cache.put(key, _request(), _response())


def test_a_failed_write_keeps_the_previous_entry(
    tmp_path: Path, cache: ResponseCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = cache.key(_request())
    cache.put(key, _request(), _response())

    def fail(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        cache.put(key, _request(), _response('{"value": 2}'))
    monkeypatch.undo()

    assert cache.get(key) == replace(_response(), from_cache=True)
    assert _entries(tmp_path / "v1" / "llm") == [f"{key}.json"]


async def test_a_miss_calls_inner_and_stores_the_response(
    tmp_path: Path, cache: ResponseCache
) -> None:
    inner = FakeLLMClient({"critic": ['{"value": 7}']})

    response = await CachingLLMClient(inner, cache).generate(_request())

    assert response.text == '{"value": 7}'
    assert not response.from_cache
    assert len(inner.requests) == 1
    assert _entries(tmp_path / "v1" / "llm") == [f"{cache.key(_request())}.json"]


async def test_a_repeat_is_answered_from_the_cache(cache: ResponseCache) -> None:
    inner = FakeLLMClient({"critic": ['{"value": 7}']})
    client = CachingLLMClient(inner, cache)
    first = await client.generate(_request())

    repeat = await client.generate(_request(purpose="critic.speedrunner"))

    assert repeat == replace(first, from_cache=True)
    assert len(inner.requests) == 1


async def test_cached_responses_survive_a_restart(tmp_path: Path, clip: MediaInput) -> None:
    request = _request(media=(clip,))
    directory = tmp_path / "v2" / "llm"
    first = await CachingLLMClient(
        FakeLLMClient({"critic": ['{"value": 3}']}), ResponseCache(directory, MODEL)
    ).generate(request)

    unused = FakeLLMClient()
    repeat = await CachingLLMClient(unused, ResponseCache(directory, MODEL)).generate(request)

    assert repeat == replace(first, from_cache=True)
    assert unused.requests == []


async def test_other_models_do_not_share_entries(tmp_path: Path) -> None:
    inner = FakeLLMClient(responder=lambda request: Answer(value=1))
    directory = tmp_path / "llm"
    await CachingLLMClient(inner, ResponseCache(directory, "model-a")).generate(_request())

    response = await CachingLLMClient(inner, ResponseCache(directory, "model-b")).generate(
        _request()
    )

    assert not response.from_cache
    assert len(inner.requests) == 2


async def test_failures_are_not_cached(tmp_path: Path, cache: ResponseCache) -> None:
    inner = FakeLLMClient({"critic": [LLMError("overloaded"), '{"value": 5}']})
    client = CachingLLMClient(inner, cache)

    with pytest.raises(LLMError, match="overloaded"):
        await client.generate(_request())
    assert not (tmp_path / "v1" / "llm").exists()

    response = await client.generate(_request())
    assert response.text == '{"value": 5}'
    assert len(inner.requests) == 2


async def test_an_unwritable_cache_still_returns_the_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "llm"
    blocker.write_text("not a directory", encoding="utf-8")
    inner = FakeLLMClient({"critic": ['{"value": 9}', '{"value": 9}']})
    client = CachingLLMClient(inner, ResponseCache(blocker, MODEL))

    with caplog.at_level(logging.WARNING, logger="iterplay.llm.cache"):
        response = await client.generate(_request())

    assert response.text == '{"value": 9}'
    assert "Could not cache the critic response" in caplog.text
