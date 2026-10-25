"""Shared fixtures, and skipping of tests whose external tools are missing.

The HLD's example payloads are used as contract fixtures. Tests marked ``browser`` or
``ffmpeg`` are skipped when Playwright's Chromium or ffmpeg/ffprobe are not installed.
"""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


def _ffmpeg_installed() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


_TOOL_PROBES: dict[str, Callable[[], bool]] = {
    "browser": _chromium_installed,
    "ffmpeg": _ffmpeg_installed,
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    available: dict[str, bool] = {}
    for item in items:
        for marker, probe in _TOOL_PROBES.items():
            if item.get_closest_marker(marker) is None:
                continue
            if marker not in available:
                available[marker] = probe()
            if not available[marker]:
                item.add_marker(pytest.mark.skip(reason=f"needs {marker}, which is not installed"))


@pytest.fixture
def hld_state() -> dict[str, Any]:
    """The ``getState()`` example from HLD §3.3 as a 2D game reports it (no z)."""
    return {
        "t": 12.34,
        "player": {"x": 4, "y": 2.45, "vx": 7, "vy": 0, "grounded": True},
        "goal": {"x": 97.5, "y": 3},
        "collectibles": [{"id": "star-1", "x": 22.5, "y": 6.5, "collected": False}],
        "deaths": 0,
        "levelComplete": False,
        "fps": 58.7,
        "events": [{"type": "collect", "t": 12.3, "id": "star-0"}],
    }


@pytest.fixture
def hld_critique() -> dict[str, Any]:
    """The critic output example from HLD §4 [5]."""
    return {
        "issues": [
            {
                "id": "I3",
                "bot": "speedrunner",
                "timestamp": "00:42",
                "rubric_item": "collisions",
                "severity": "high",
                "observation": "Player passes through the second wall and falls below the level.",
                "evidence_type": "visual+telemetry",
                "suspected_cause": "Wall collider not added to colliders[] in level.js",
                "fix_hint": "Register wall meshes with the collision system",
            }
        ],
        "feel_notes": ["Jump arc looks floaty vs design doc 'snappy' target"],
        "rubric_assessment": {"goal_clarity": 2, "feel": 3},
    }
