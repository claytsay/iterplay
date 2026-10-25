"""Per-version score (HLD §4 [7])."""

from pydantic import Field

from iterplay.models.base import StrictModel
from iterplay.models.rubric import RubricSource


class ItemScore(StrictModel):
    item_id: str
    source: RubricSource
    weight: float = Field(gt=0)
    score: float | None = Field(
        ge=0,
        le=1,
        description="Normalised score; None when not measurable (excluded from the total).",
    )
    detail: str = ""


class ScoreCard(StrictModel):
    """Contents of ``score.json``."""

    version: int = Field(ge=1)
    items: list[ItemScore]
    total: float = Field(ge=0, le=1, description="Weighted mean of the measured items.")
    target: float = Field(gt=0, le=1)
    meets_target: bool
