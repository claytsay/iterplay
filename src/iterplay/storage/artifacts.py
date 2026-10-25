"""JSON and text persistence for run artifacts.

Writes are atomic (temporary file, then rename), so a crash never leaves a half-written
artifact for replay or the viewer to trip over.
"""

import os
import uuid
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def write_model(path: Path, model: BaseModel) -> None:
    """Write ``model`` as indented JSON, creating parent directories.

    Field names follow each model's own alias settings, e.g. ``GameState``'s ``levelComplete``.
    """
    _write_atomic(path, (model.model_dump_json(indent=2) + "\n").encode("utf-8"))


def read_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a JSON artifact.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        pydantic.ValidationError: The file does not match ``model_type``.
    """
    return model_type.model_validate_json(path.read_bytes())


def write_text(path: Path, text: str) -> None:
    """Write a UTF-8 text artifact such as ``changelog.md``, creating parent directories."""
    _write_atomic(path, text.encode("utf-8"))


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a hidden temporary file beside ``path``, then rename it into place.

    The temporary file is in the same directory, so the rename never crosses file systems and
    readers see either the old contents or the new, never a mixture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        with temporary.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        if created:
            temporary.unlink(missing_ok=True)
        raise
