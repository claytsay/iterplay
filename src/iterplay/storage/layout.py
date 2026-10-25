"""On-disk layout of a run (HLD §6). Pure path arithmetic: nothing here touches the disk."""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from iterplay.contract.layout import GAME_DIR
from iterplay.models.playtest import BotName

RUN_INDEX_FILE: Final = "index.json"
"""Written into the runs directory for the viewer."""


@dataclass(frozen=True, slots=True)
class VersionPaths:
    """Artifacts of one version: ``runs/<run_id>/v<N>/``."""

    root: Path

    @property
    def game_dir(self) -> Path:
        """The exact files the Builder or Fixer produced, plus the vendored ``vendor/``."""
        return self.root / GAME_DIR

    @property
    def smoke_report(self) -> Path:
        return self.root / "smoke.json"

    @property
    def playtest_dir(self) -> Path:
        return self.root / "playtest"

    @property
    def playtest_report(self) -> Path:
        return self.playtest_dir / "report.json"

    def raw_video(self, bot: BotName) -> Path:
        """The 1280x720 recording of one bot run."""
        return self.playtest_dir / f"{bot}.webm"

    def telemetry(self, bot: BotName) -> Path:
        return self.playtest_dir / f"{bot}.telemetry.jsonl"

    @property
    def console_log(self) -> Path:
        """Console output of every run, each line prefixed with the bot name."""
        return self.playtest_dir / "console.log"

    @property
    def telemetry_summary(self) -> Path:
        return self.playtest_dir / "telemetry_summary.json"

    def prepared_video(self, bot: BotName) -> Path:
        """One run, labelled, timestamped and downscaled for a persona critic."""
        return self.playtest_dir / f"{bot}_360p.mp4"

    @property
    def combined_video(self) -> Path:
        """Every run back to back with title cards, as sent to the critic."""
        return self.playtest_dir / "combined_360p.mp4"

    @property
    def critique_raw(self) -> Path:
        return self.root / "critique.raw.json"

    @property
    def critique_verified(self) -> Path:
        return self.root / "critique.verified.json"

    @property
    def score(self) -> Path:
        return self.root / "score.json"

    @property
    def changelog(self) -> Path:
        """Patch notes tying each change to an issue id; empty for v1."""
        return self.root / "changelog.md"

    @property
    def llm_dir(self) -> Path:
        """Raw prompts and responses keyed by request hash, for caching and replay."""
        return self.root / "llm"


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Artifacts of one run: ``runs/<run_id>/``."""

    root: Path

    @property
    def run_id(self) -> str:
        return self.root.name

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def design_doc(self) -> Path:
        return self.root / "design_doc.md"

    @property
    def summary(self) -> Path:
        return self.root / "summary.json"

    def version(self, number: int) -> VersionPaths:
        return VersionPaths(self.root / f"v{number}")
