"""Run-level configuration and bookkeeping (HLD §4 [7], §6, §8)."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from iterplay.contract.templates import DEFAULT_TEMPLATE, TemplateId
from iterplay.models.base import StrictModel
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric


class CriticBackendKind(StrEnum):
    """Which ``CriticBackend`` implementation to use (HLD §5)."""

    DIRECT = "direct"
    MANAGED = "managed"


class Budgets(StrictModel):
    """Limits that end a run early (HLD §4 [7], §8)."""

    max_iterations: int = Field(default=5, ge=1, description="Versions to build, v1 included.")
    max_wall_time_s: float | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_spend_usd: float | None = Field(default=None, gt=0)


class ModelPricing(StrictModel):
    """Prices that turn token usage into spend; required to enforce ``max_spend_usd``."""

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)


class RunConfig(StrictModel):
    """Contents of ``config.json``: everything needed to reproduce a run."""

    run_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="Also the run's directory name.",
    )
    created_at: datetime
    design_doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template: TemplateId = Field(default=DEFAULT_TEMPLATE, description="Kind of game to build.")
    rubric: Rubric
    model: str
    game_seed: int = Field(
        default=1,
        description="Level seed shared by every bot and version, so their runs compare.",
    )
    bot_seeds: dict[BotName, int] = Field(description="Seeds for each bot's own randomness.")
    budgets: Budgets = Field(default_factory=Budgets)
    pricing: ModelPricing | None = None
    critic_backend: CriticBackendKind = CriticBackendKind.DIRECT
    headless: bool = Field(
        default=True,
        description="Headless browsers use software WebGL, so performance is not scored.",
    )


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class VersionOutcome(StrEnum):
    """What the keep-best guard decided about a version."""

    ACCEPTED = "accepted"
    """Scored at least as well as the best so far; the next fix starts from it."""

    REVERTED = "reverted"
    """Scored below the best; the next fix starts from the best version instead."""

    BUILD_FAILED = "build_failed"
    """Failed the smoke test after every quick retry."""


class StopReason(StrEnum):
    TARGET_REACHED = "target_reached"
    MAX_ITERATIONS = "max_iterations"
    TIME_BUDGET = "time_budget"
    TOKEN_BUDGET = "token_budget"
    SPEND_BUDGET = "spend_budget"


class VersionRecord(StrictModel):
    """One version's entry in ``summary.json``."""

    version: int = Field(ge=1)
    parent_version: int | None = Field(description="Version the Fixer started from; None for v1.")
    score: float = Field(ge=0, le=1)
    outcome: VersionOutcome
    duration_s: float = Field(ge=0)
    token_usage: TokenUsage


class RunSummary(StrictModel):
    """Contents of ``summary.json``, rewritten after every version so a crashed run replays."""

    run_id: str
    versions: list[VersionRecord] = Field(default_factory=list)
    best_version: int | None = None
    stop_reason: StopReason | None = Field(default=None, description="None while running.")
    duration_s: float = Field(default=0, ge=0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    spend_usd: float | None = None


class RunIndex(StrictModel):
    """Contents of ``runs/index.json``, which the viewer reads to discover runs."""

    run_ids: list[str]
