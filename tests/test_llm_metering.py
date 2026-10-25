"""Usage metering enforces a run's token and spend budgets (HLD §8); cache hits stay free."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from iterplay.errors import BudgetExceededError, LLMError
from iterplay.llm.base import LLMRequest
from iterplay.llm.cache import CachingLLMClient, ResponseCache
from iterplay.llm.metering import MeteredLLMClient, UsageMeter
from iterplay.models.run import Budgets, ModelPricing, StopReason, TokenUsage
from tests.helpers import FakeLLMClient

PRICING = ModelPricing(input_usd_per_mtok=1.25, output_usd_per_mtok=10.0)


class Answer(BaseModel):
    value: int


def _request(prompt: str = "What is the answer?", purpose: str = "builder") -> LLMRequest:
    return LLMRequest(
        purpose=purpose,
        system_instruction="Answer in JSON.",
        prompt=prompt,
        response_schema=Answer,
    )


def _usage(input_tokens: int, output_tokens: int) -> TokenUsage:
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def test_meter_starts_empty() -> None:
    meter = UsageMeter(Budgets(), PRICING)

    assert meter.total == TokenUsage()
    assert meter.spend_usd == 0
    assert meter.exhausted() is None


def test_meter_accumulates_usage() -> None:
    meter = UsageMeter(Budgets(), None)

    meter.record(_usage(100, 20))
    meter.record(_usage(5, 7))

    assert meter.total == _usage(105, 27)


def test_spend_uses_per_million_token_prices() -> None:
    meter = UsageMeter(Budgets(), PRICING)

    meter.record(_usage(1_000_000, 200_000))

    assert meter.spend_usd == pytest.approx(1.25 + 2.0)


def test_spend_is_unknown_without_pricing() -> None:
    meter = UsageMeter(Budgets(), None)

    meter.record(_usage(1_000_000, 1_000_000))

    assert meter.spend_usd is None


def test_no_limits_never_exhaust() -> None:
    meter = UsageMeter(Budgets(), PRICING)

    meter.record(_usage(10**12, 10**12))

    assert meter.exhausted() is None


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (_usage(60, 39), None),
        (_usage(60, 40), StopReason.TOKEN_BUDGET),
        (_usage(100, 0), StopReason.TOKEN_BUDGET),
        (_usage(0, 150), StopReason.TOKEN_BUDGET),
    ],
)
def test_token_budget_counts_input_and_output(
    usage: TokenUsage, expected: StopReason | None
) -> None:
    meter = UsageMeter(Budgets(max_total_tokens=100), None)

    meter.record(usage)

    assert meter.exhausted() is expected


@pytest.mark.parametrize(
    ("output_tokens", "expected"),
    [(99_999, None), (100_000, StopReason.SPEND_BUDGET), (500_000, StopReason.SPEND_BUDGET)],
)
def test_spend_budget_is_reached_at_the_limit(
    output_tokens: int, expected: StopReason | None
) -> None:
    meter = UsageMeter(Budgets(max_spend_usd=1.0), PRICING)

    meter.record(_usage(0, output_tokens))

    assert meter.exhausted() is expected


def test_spend_budget_needs_pricing() -> None:
    meter = UsageMeter(Budgets(max_spend_usd=0.01), None)

    meter.record(_usage(10**9, 10**9))

    assert meter.exhausted() is None


def test_token_budget_is_reported_first() -> None:
    meter = UsageMeter(Budgets(max_total_tokens=10, max_spend_usd=0.01), PRICING)

    meter.record(_usage(10**9, 10**9))

    assert meter.exhausted() is StopReason.TOKEN_BUDGET


async def test_metered_client_records_each_call() -> None:
    meter = UsageMeter(Budgets(), None)
    client = MeteredLLMClient(FakeLLMClient({"builder": ['{"value": 1}', '{"value": 2}']}), meter)

    first = await client.generate(_request())
    second = await client.generate(_request("Again?"))

    assert meter.total == _usage(
        first.usage.input_tokens + second.usage.input_tokens,
        first.usage.output_tokens + second.usage.output_tokens,
    )


async def test_the_call_that_crosses_the_budget_completes_and_the_next_is_refused() -> None:
    meter = UsageMeter(Budgets(max_total_tokens=5), None)
    inner = FakeLLMClient(responder=lambda request: Answer(value=42))
    client = MeteredLLMClient(inner, meter)

    response = await client.generate(_request("x" * 100))
    with pytest.raises(BudgetExceededError, match="token_budget") as refused:
        await client.generate(_request(purpose="fixer"))

    assert response.text == '{"value":42}'
    assert len(inner.requests) == 1
    assert "'fixer'" in str(refused.value)


async def test_spend_budget_refusals_name_the_reason() -> None:
    meter = UsageMeter(Budgets(max_spend_usd=0.5), PRICING)
    meter.record(_usage(0, 50_000))
    inner = FakeLLMClient(responder=lambda request: Answer(value=1))

    with pytest.raises(BudgetExceededError, match="spend_budget"):
        await MeteredLLMClient(inner, meter).generate(_request())

    assert inner.requests == []


async def test_failed_calls_record_nothing() -> None:
    meter = UsageMeter(Budgets(), None)
    client = MeteredLLMClient(FakeLLMClient({"builder": [LLMError("boom")]}), meter)

    with pytest.raises(LLMError, match="boom"):
        await client.generate(_request())

    assert meter.total == TokenUsage()


async def test_responses_from_a_cache_are_not_recorded(tmp_path: Path) -> None:
    meter = UsageMeter(Budgets(), None)
    inner = FakeLLMClient(responder=lambda request: Answer(value=1))
    client = MeteredLLMClient(CachingLLMClient(inner, ResponseCache(tmp_path, "m")), meter)

    first = await client.generate(_request())
    repeat = await client.generate(_request())

    assert repeat.from_cache
    assert meter.total == first.usage


async def test_cache_hits_stay_free_after_the_budget_is_spent(tmp_path: Path) -> None:
    meter = UsageMeter(Budgets(max_total_tokens=1), None)
    inner = FakeLLMClient(responder=lambda request: Answer(value=1))
    client = CachingLLMClient(MeteredLLMClient(inner, meter), ResponseCache(tmp_path, "m"))
    await client.generate(_request())
    spent = meter.total

    repeat = await client.generate(_request(purpose="builder.retry"))
    with pytest.raises(BudgetExceededError):
        await client.generate(_request("A new question"))

    assert repeat.from_cache
    assert meter.total == spent
    assert len(inner.requests) == 1
