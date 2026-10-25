"""The shipped rubric.yaml is valid and covers every item in HLD §4 [7]."""

from pathlib import Path

import pytest
import yaml

from iterplay.models.rubric import Rubric, RubricItemId, RubricSource

RUBRIC_FILE = Path(__file__).resolve().parents[1] / "rubric.yaml"


@pytest.fixture
def rubric() -> Rubric:
    return Rubric.model_validate(yaml.safe_load(RUBRIC_FILE.read_text(encoding="utf-8")))


def test_rubric_covers_every_known_item(rubric: Rubric) -> None:
    assert [item.id for item in rubric.items] == [item.value for item in RubricItemId]


def test_rubric_is_mostly_objective(rubric: Rubric) -> None:
    critic = sum(item.weight for item in rubric.items if item.source is RubricSource.CRITIC)
    total = sum(item.weight for item in rubric.items)

    assert critic < total / 2
