"""Loading ``rubric.yaml`` (HLD §4 [7])."""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from iterplay.errors import ConfigError
from iterplay.models.rubric import Rubric, RubricItemId, RubricSource

_KNOWN_ITEM_IDS: Final = frozenset(item_id.value for item_id in RubricItemId)


def load_rubric(path: Path) -> Rubric:
    """Parse and validate a rubric file.

    Beyond the schema, item ids must be unique, and every objective item (any source except
    ``critic``) must use a :class:`~iterplay.models.rubric.RubricItemId`.

    Raises:
        ConfigError: The file is missing, is not YAML, or is invalid.
    """
    try:
        with path.open("rb") as stream:
            data = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Cannot read rubric {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Rubric {path} is not valid YAML: {exc}") from exc
    if data is None:
        raise ConfigError(f"Rubric {path} is empty")
    try:
        rubric = Rubric.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Rubric {path} is invalid: {_describe(exc)}") from exc
    problems = _item_problems(rubric)
    if problems:
        raise ConfigError(f"Rubric {path} is invalid: {'; '.join(problems)}")
    return rubric


def _item_problems(rubric: Rubric) -> list[str]:
    """Rules the schema cannot express: unique ids, and known ids for objective items."""
    problems = [
        f"item id {item_id!r} is used {count} times; ids must be unique"
        for item_id, count in Counter(item.id for item in rubric.items).items()
        if count > 1
    ]
    problems.extend(
        f"item {item.id!r} has source {item.source.value!r}, but objective items must use an "
        f"id the Scorer knows: {', '.join(RubricItemId)}"
        for item in rubric.items
        if item.source is not RubricSource.CRITIC and item.id not in _KNOWN_ITEM_IDS
    )
    return problems


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{_location(detail['loc'])}: {detail['msg']}" if detail["loc"] else detail["msg"]
        for detail in error.errors()
    )


def _location(loc: Sequence[int | str]) -> str:
    """``("items", 2, "weight")`` as ``items[2].weight``."""
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text
