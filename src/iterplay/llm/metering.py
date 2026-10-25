"""Token and spend accounting against a run's budgets (HLD §8)."""

from iterplay.errors import BudgetExceededError
from iterplay.llm.base import LLMClient, LLMRequest, LLMResponse
from iterplay.models.run import Budgets, ModelPricing, StopReason, TokenUsage


class UsageMeter:
    """Accumulates the usage of real (uncached) model calls for one run."""

    def __init__(self, budgets: Budgets, pricing: ModelPricing | None) -> None:
        self._budgets = budgets
        self._pricing = pricing
        self._total = TokenUsage()

    @property
    def total(self) -> TokenUsage:
        return self._total

    @property
    def spend_usd(self) -> float | None:
        """Spend so far, or None when no pricing is configured."""
        if self._pricing is None:
            return None
        return (
            self._total.input_tokens * self._pricing.input_usd_per_mtok
            + self._total.output_tokens * self._pricing.output_usd_per_mtok
        ) / 1e6

    def record(self, usage: TokenUsage) -> None:
        self._total = TokenUsage(
            input_tokens=self._total.input_tokens + usage.input_tokens,
            output_tokens=self._total.output_tokens + usage.output_tokens,
        )

    def exhausted(self) -> StopReason | None:
        """``TOKEN_BUDGET`` or ``SPEND_BUDGET`` once a limit is reached, otherwise None.

        The token budget is checked first. The spend budget only applies when pricing is
        configured.
        """
        max_tokens = self._budgets.max_total_tokens
        used = self._total.input_tokens + self._total.output_tokens
        if max_tokens is not None and used >= max_tokens:
            return StopReason.TOKEN_BUDGET
        max_spend = self._budgets.max_spend_usd
        spend = self.spend_usd
        if max_spend is not None and spend is not None and spend >= max_spend:
            return StopReason.SPEND_BUDGET
        return None


class MeteredLLMClient:
    """:class:`~iterplay.llm.base.LLMClient` decorator that records usage and refuses calls
    once the budget is spent. Place it inside the cache so cache hits stay free.
    """

    def __init__(self, inner: LLMClient, meter: UsageMeter) -> None:
        self._inner = inner
        self._meter = meter

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Raises ``BudgetExceededError`` instead of calling ``inner`` once exhausted.

        The budget is checked before each call, so the call that crosses a limit completes
        and the next one is refused. Responses marked ``from_cache`` are not recorded.
        """
        reason = self._meter.exhausted()
        if reason is not None:
            raise BudgetExceededError(
                f"Budget exhausted ({reason.value}); refusing the {request.purpose!r} model call"
            )
        response = await self._inner.generate(request)
        if not response.from_cache:
            self._meter.record(response.usage)
        return response
