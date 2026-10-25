"""Creates, opens and lists runs in the runs directory (HLD §6)."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from iterplay.errors import RunNotFoundError
from iterplay.models.run import RunConfig, RunIndex
from iterplay.storage.artifacts import read_model, write_model, write_text
from iterplay.storage.layout import RUN_INDEX_FILE, RunPaths

_log = logging.getLogger(__name__)


class RunStore:
    """Owns the runs directory (``runs/`` by default)."""

    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = runs_dir

    def create(self, config: RunConfig, design_doc: str) -> RunPaths:
        """Create ``runs/<run_id>/`` and write ``config.json`` and ``design_doc.md``.

        The runs directory is created if needed. If a write fails, the new run directory is
        removed again, so the id can be reused.

        Raises:
            FileExistsError: A run with this id already exists.
        """
        run = RunPaths(self._runs_dir / config.run_id)
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        run.root.mkdir()
        try:
            write_model(run.config, config)
            write_text(run.design_doc, design_doc)
        except BaseException:
            shutil.rmtree(run.root, ignore_errors=True)
            raise
        return run

    def open(self, run_dir: Path) -> RunPaths:
        """Return the layout of an existing run.

        Raises:
            RunNotFoundError: ``run_dir`` has no ``config.json``.
        """
        run = RunPaths(run_dir)
        if not run.config.is_file():
            raise RunNotFoundError(f"{run_dir} is not a run: it has no {run.config.name}")
        return run

    def list_run_ids(self) -> list[str]:
        """Ids of every run that has a readable ``config.json``, oldest first.

        Runs are ordered by ``created_at`` (naive times count as UTC), then by id. Hidden
        directories are skipped, because the viewer never serves them.
        """
        if not self._runs_dir.is_dir():
            return []
        runs: list[tuple[datetime, str]] = []
        for entry in self._runs_dir.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            created_at = _created_at(RunPaths(entry))
            if created_at is not None:
                runs.append((created_at, entry.name))
        return [run_id for _, run_id in sorted(runs)]

    def write_index(self) -> Path:
        """Write ``runs/index.json`` (a ``RunIndex``) for the viewer and return its path.

        The runs directory is created if needed.
        """
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        path = self._runs_dir / RUN_INDEX_FILE
        write_model(path, RunIndex(run_ids=self.list_run_ids()))
        return path


def _created_at(run: RunPaths) -> datetime | None:
    """When ``run`` was created, as an aware time, or None if its config cannot be read."""
    if not run.config.is_file():
        return None
    try:
        config = read_model(run.config, RunConfig)
    except (OSError, ValidationError) as exc:
        _log.warning("Skipping run %s: its %s is unreadable: %s", run.root, run.config.name, exc)
        return None
    created_at = config.created_at
    return created_at if created_at.utcoffset() is not None else created_at.replace(tzinfo=UTC)
