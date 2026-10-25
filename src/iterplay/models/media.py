"""Prepared clips handed to the critic (HLD §4 [4])."""

from pathlib import Path

from pydantic import Field

from iterplay.models.base import StrictModel
from iterplay.models.playtest import BotName


class PreparedMedia(StrictModel):
    combined: Path = Field(description="Every run back to back, separated by title cards.")
    per_bot: dict[BotName, Path] = Field(description="Each run alone, for persona critics.")
