"""Loading ``rubric.yaml`` (HLD §4 [7])."""

from pathlib import Path

from iterplay.models.rubric import Rubric


def load_rubric(path: Path) -> Rubric:
    """Parse and validate a rubric file.

    Beyond the schema, item ids must be unique, and every objective item (any source except
    ``critic``) must use a :class:`~iterplay.models.rubric.RubricItemId`.

    Raises:
        ConfigError: The file is missing, is not YAML, or is invalid.
    """
    raise NotImplementedError
