"""Scoring rubric, loaded from ``rubric.yaml`` (HLD §4 [7])."""

from enum import StrEnum

from pydantic import Field

from iterplay.models.base import StrictModel


class RubricSource(StrEnum):
    """Where an item's score comes from. Every source except ``critic`` is objective."""

    SMOKE = "smoke"
    CONSOLE = "console"
    TELEMETRY = "telemetry"
    CRITIC = "critic"


class RubricItemId(StrEnum):
    """Items the Scorer and Verifier know how to evaluate.

    Objective items must use one of these ids. Critic items may use any id, because the critic
    scores them 0-5 by id.
    """

    BOOTS_CLEANLY = "boots_cleanly"
    NO_RUNTIME_ERRORS = "no_runtime_errors"
    COMPLETABLE = "completable"
    COLLECTIBLES_REACHABLE = "collectibles_reachable"
    COLLISION_INTEGRITY = "collision_integrity"
    FAIR_DIFFICULTY = "fair_difficulty"
    PERFORMANCE = "performance"
    MATCHES_DESIGN_DOC = "matches_design_doc"
    GAME_FEEL = "game_feel"


class RubricItem(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    source: RubricSource
    weight: float = Field(gt=0)
    params: dict[str, float] = Field(
        default_factory=dict,
        description="Item-specific thresholds, e.g. max_deaths or min_fps.",
    )


class Rubric(StrictModel):
    target: float = Field(gt=0, le=1, description="Stop once a version's score reaches this.")
    items: list[RubricItem] = Field(min_length=1)
