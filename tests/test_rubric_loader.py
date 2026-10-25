"""Rubric files are parsed, validated and checked beyond their schema (HLD §4 [7])."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from iterplay.errors import ConfigError
from iterplay.evaluation.rubric import load_rubric
from iterplay.models.rubric import RubricItemId, RubricSource

REPO_RUBRIC = Path(__file__).resolve().parents[1] / "rubric.yaml"


def item(item_id: str, source: str = "telemetry", **fields: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "description": f"About {item_id}.",
        "source": source,
        "weight": 1.0,
        **fields,
    }


def write_rubric(path: Path, items: list[dict[str, Any]], target: float = 0.9) -> Path:
    path.write_text(yaml.safe_dump({"target": target, "items": items}), encoding="utf-8")
    return path


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "rubric.yaml"


def test_the_repository_rubric_loads() -> None:
    rubric = load_rubric(REPO_RUBRIC)

    assert rubric.target == 0.9
    assert [item.id for item in rubric.items] == list(RubricItemId)
    params = {item.id: item.params for item in rubric.items if item.params}
    assert params == {"fair_difficulty": {"max_deaths": 3}, "performance": {"min_fps": 30}}


def test_critic_items_may_use_any_id(path: Path) -> None:
    write_rubric(path, [item("completable", weight=2), item("goal_clarity", source="critic")])

    rubric = load_rubric(path)

    assert [(item.id, item.source, item.weight) for item in rubric.items] == [
        ("completable", RubricSource.TELEMETRY, 2.0),
        ("goal_clarity", RubricSource.CRITIC, 1.0),
    ]


def test_a_missing_file_raises_config_error_naming_it(path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read") as excinfo:
        load_rubric(path)

    assert str(path) in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)


def test_a_directory_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        load_rubric(tmp_path)


@pytest.mark.parametrize(
    "content", [b"items: [unclosed\n", b"target: 0.9\n\titems: []\n", b"target: \x80\n"]
)
def test_a_file_that_is_not_yaml_raises_config_error(path: Path, content: bytes) -> None:
    path.write_bytes(content)

    with pytest.raises(ConfigError, match="not valid YAML") as excinfo:
        load_rubric(path)

    assert str(path) in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, yaml.YAMLError)


@pytest.mark.parametrize("content", ["", "# no items yet\n"])
def test_an_empty_file_raises_config_error(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="empty"):
        load_rubric(path)


@pytest.mark.parametrize(
    ("document", "location"),
    [
        ({"target": 1.5, "items": [item("completable")]}, "target"),
        ({"target": 0.9, "items": []}, "items"),
        ({"target": 0.9, "items": [item("completable", weight=0)]}, "items[0].weight"),
        ({"target": 0.9, "items": [item("completable", source="vibes")]}, "items[0].source"),
        ({"target": 0.9, "items": [item("Not-An-Id", source="critic")]}, "items[0].id"),
        ({"target": 0.9, "items": [item("completable", colour="red")]}, "items[0].colour"),
        ({"target": 0.9, "items": [item("performance", params={"min_fps": "fast"})]}, "min_fps"),
        ({"items": [item("completable")]}, "target"),
        (["target", "items"], "valid dictionary"),
    ],
)
def test_schema_violations_raise_config_error_naming_the_field(
    path: Path, document: object, location: str
) -> None:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ConfigError, match="is invalid") as excinfo:
        load_rubric(path)

    assert str(path) in str(excinfo.value)
    assert location in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_item_ids_must_be_unique(path: Path) -> None:
    write_rubric(path, [item("completable"), item("goal_clarity", source="critic")] * 2)

    with pytest.raises(ConfigError) as excinfo:
        load_rubric(path)

    message = str(excinfo.value)
    assert "'completable' is used 2 times" in message
    assert "'goal_clarity' is used 2 times" in message


@pytest.mark.parametrize("source", ["smoke", "console", "telemetry"])
def test_objective_items_must_use_an_id_the_scorer_knows(path: Path, source: str) -> None:
    write_rubric(path, [item("completable"), item("collisions", source=source)])

    with pytest.raises(ConfigError, match="'collisions'") as excinfo:
        load_rubric(path)

    assert str(path) in str(excinfo.value)
    assert "collision_integrity" in str(excinfo.value)


def test_every_problem_is_reported_at_once(path: Path) -> None:
    write_rubric(path, [item("completable"), item("completable"), item("speed")])

    with pytest.raises(ConfigError) as excinfo:
        load_rubric(path)

    assert "'completable' is used 2 times" in str(excinfo.value)
    assert "'speed'" in str(excinfo.value)
