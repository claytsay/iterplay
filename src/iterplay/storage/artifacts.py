"""JSON and text persistence for run artifacts.

Writes are atomic (temporary file, then rename), so a crash never leaves a half-written
artifact for replay or the viewer to trip over.
"""

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def write_model(path: Path, model: BaseModel) -> None:
    """Write ``model`` as indented JSON, creating parent directories."""
    raise NotImplementedError


def read_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a JSON artifact.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        pydantic.ValidationError: The file does not match ``model_type``.
    """
    raise NotImplementedError


def write_text(path: Path, text: str) -> None:
    """Write a UTF-8 text artifact such as ``changelog.md``, creating parent directories."""
    raise NotImplementedError
