"""The game contract accepts what HLD §3 describes and rejects what it forbids."""

from typing import Any

import pytest
from pydantic import ValidationError

from iterplay.contract.layout import ALLOWED_PATH
from iterplay.contract.state import GameEventType, GameState


def test_game_state_accepts_hld_example(hld_state: dict[str, Any]) -> None:
    state = GameState.model_validate(hld_state)

    assert state.level_complete is False
    assert state.events[0].type is GameEventType.COLLECT
    assert state.events[0].id == "star-0"


def test_2d_games_may_omit_depth(hld_state: dict[str, Any]) -> None:
    state = GameState.model_validate(hld_state)

    assert (state.player.z, state.player.vz, state.goal.z, state.collectibles[0].z) == (0, 0, 0, 0)


def test_3d_positions_fit_the_same_schema(hld_state: dict[str, Any]) -> None:
    player = {**hld_state["player"], "z": -2.0, "vz": -3.5}
    goal = {"x": 1, "y": 1, "z": 9}

    state = GameState.model_validate({**hld_state, "player": player, "goal": goal})

    assert (state.player.z, state.player.vz, state.goal.z) == (-2.0, -3.5, 9)


def test_game_state_serialises_with_javascript_field_names(hld_state: dict[str, Any]) -> None:
    dumped = GameState.model_validate(hld_state).model_dump(mode="json")

    assert dumped["levelComplete"] is False
    assert "level_complete" not in dumped


def test_game_state_tolerates_extra_fields_and_numeric_ids(hld_state: dict[str, Any]) -> None:
    payload = {
        **hld_state,
        "debug": {"colliders": 12},
        "collectibles": [{"id": 7, "x": 0, "y": 0, "collected": True}],
    }

    assert GameState.model_validate(payload).collectibles[0].id == "7"


def test_game_state_keeps_event_specific_fields(hld_state: dict[str, Any]) -> None:
    payload = {**hld_state, "events": [{"type": "death", "t": 3.0, "cause": "spikes"}]}

    event = GameState.model_validate(payload).events[0]

    assert event.model_extra == {"cause": "spikes"}


@pytest.mark.parametrize(
    "change",
    [
        {"events": [{"type": "teleport", "t": 1.0}]},
        {"deaths": -1},
        {"player": {"x": 0, "y": 0}},
    ],
)
def test_game_state_rejects_contract_violations(
    hld_state: dict[str, Any], change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        GameState.model_validate({**hld_state, **change})


@pytest.mark.parametrize("path", ["index.html", "main.js", "level_1.js", "player-physics.js"])
def test_allowlist_accepts_flat_game_files(path: str) -> None:
    assert ALLOWED_PATH.fullmatch(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../main.js",
        "/etc/passwd",
        "C:\\main.js",
        "vendor/three.module.js",
        "src/player.js",
        ".hidden.js",
        "main.ts",
        "index.htm",
        "main.js\n",
    ],
)
def test_allowlist_rejects_everything_else(path: str) -> None:
    assert ALLOWED_PATH.fullmatch(path) is None


@pytest.mark.parametrize(
    "change",
    [
        {"player": {"x": float("nan"), "y": 0, "vx": 0, "vy": 0, "grounded": True}},
        {"fps": float("inf")},
        {"goal": {"x": 1, "y": float("-inf")}},
    ],
)
def test_game_state_rejects_non_finite_numbers(
    hld_state: dict[str, Any], change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        GameState.model_validate({**hld_state, **change})
