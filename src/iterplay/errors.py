"""Exception hierarchy shared by every iterplay package."""


class IterplayError(Exception):
    """Base class for all errors raised by iterplay."""


class ConfigError(IterplayError):
    """Settings, run config, or rubric are missing or invalid."""


class GuardrailError(IterplayError):
    """Generated game files break a hard guardrail (HLD §4 [1]).

    For example: a required file is missing, a path repeats, or a size cap is exceeded.
    """


class UnsafePathError(GuardrailError):
    """A path would escape its sandbox directory: absolute, ``..``, or via a symlink (HLD §9)."""


class ContractError(IterplayError):
    """A game build does not honour the ``window.iterplay`` harness contract (HLD §3)."""


class LLMError(IterplayError):
    """A model call failed."""


class LLMResponseError(LLMError):
    """The model's response does not validate against the requested schema."""


class BudgetExceededError(IterplayError):
    """The run's token or spend budget is exhausted, so no further model calls are made."""


class MediaError(IterplayError):
    """An ffmpeg or ffprobe invocation failed."""


class RunNotFoundError(IterplayError):
    """A run or version directory does not exist or lacks required artifacts."""
