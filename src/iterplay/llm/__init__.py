"""Model access (HLD §2, §6, §8).

:class:`~iterplay.llm.base.LLMClient` is the only interface agents depend on. Implementations
compose as decorators, outermost first::

    CachingLLMClient(MeteredLLMClient(GeminiClient(...), meter), cache)

so cache hits are free and only real API calls count against the budget.
"""
