"""Token and spend accounting against a run's budgets (HLD §8)."""

from iterplay.llm.base import LLMClient, LLMRequest, LLMResponse
from iterplay.models.run import Budgets, ModelPricing, StopReason, TokenUsage


class UsageMeter:
    """Accumulates the usage of real (uncached) model calls for one run."""

    def __init__(self, budgets: Budgets, pricing: ModelPricing | None) -> None:
        self._budgets = budgets
        self._pricing = pricing

    @property
    def total(self) -> TokenUsage:
        raise NotImplementedError

    @property
    def spend_usd(self) -> float | None:
        """Spend so far, or None when no pricing is configured."""
        raise NotImplementedError

    def record(self, usage: TokenUsage) -> None:
        raise NotImplementedError

    def exhausted(self) -> StopReason | None:
        """``TOKEN_BUDGET`` or ``SPEND_BUDGET`` once a limit is reached, otherwise None."""
        raise NotImplementedError


class MeteredLLMClient:
    """:class:`~iterplay.llm.base.LLMClient` decorator that records usage and refuses calls
    once the budget is spent. Place it inside the cache so cache hits stay free.
    """

    def __init__(self, inner: LLMClient, meter: UsageMeter) -> None:
        self._inner = inner
        self._meter = meter

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Raises ``BudgetExceededError`` instead of calling ``inner`` once exhausted."""
        raise NotImplementedError
