"""Base classes for every pydantic model in iterplay.

Both bases write non-finite floats as the JSON strings "NaN", "Infinity" and "-Infinity",
which read back as floats, so an artifact that holds one stays valid JSON and still loads.
"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Data iterplay produces itself: stage outputs and artifacts on disk.

    Unknown fields are rejected, so schema drift between stages fails loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_inf_nan="strings")


class TolerantModel(BaseModel):
    """Data from untrusted producers: the model or a generated game.

    Unknown fields are ignored (tolerant reader), so harmless additions never fail a run.
    Required fields and types are still enforced.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", ser_json_inf_nan="strings")
