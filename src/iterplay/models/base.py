"""Base classes for every pydantic model in iterplay."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Data iterplay produces itself: stage outputs and artifacts on disk.

    Unknown fields are rejected, so schema drift between stages fails loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class TolerantModel(BaseModel):
    """Data from untrusted producers: the model or a generated game.

    Unknown fields are ignored (tolerant reader), so harmless additions never fail a run.
    Required fields and types are still enforced.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")
