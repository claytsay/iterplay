"""Creates, opens and lists runs in the runs directory (HLD §6)."""

from pathlib import Path

from iterplay.models.run import RunConfig
from iterplay.storage.layout import RunPaths


class RunStore:
    """Owns the runs directory (``runs/`` by default)."""

    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = runs_dir

    def create(self, config: RunConfig, design_doc: str) -> RunPaths:
        """Create ``runs/<run_id>/`` and write ``config.json`` and ``design_doc.md``.

        Raises:
            FileExistsError: A run with this id already exists.
        """
        raise NotImplementedError

    def open(self, run_dir: Path) -> RunPaths:
        """Return the layout of an existing run.

        Raises:
            RunNotFoundError: ``run_dir`` has no ``config.json``.
        """
        raise NotImplementedError

    def list_run_ids(self) -> list[str]:
        """Ids of every run that has a ``config.json``, oldest first."""
        raise NotImplementedError

    def write_index(self) -> Path:
        """Write ``runs/index.json`` (a ``RunIndex``) for the viewer and return its path."""
        raise NotImplementedError
