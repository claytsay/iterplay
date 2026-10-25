"""Control schemes (HLD §3.4).

Controls are fixed per game template so bots stay generic. A bot decides in terms of abstract
:class:`Control` values, and the template's :class:`ControlScheme` turns them into real
keyboard events, so bots exercise the same input path a human would.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Control(StrEnum):
    """Abstract inputs a bot can hold. ``FORWARD`` and ``BACK`` only exist in 3D templates."""

    LEFT = "left"
    RIGHT = "right"
    FORWARD = "forward"
    BACK = "back"
    JUMP = "jump"
    RESPAWN = "respawn"


@dataclass(frozen=True, slots=True)
class ControlScheme:
    """How one game template maps abstract controls onto keys and world directions."""

    bindings: Mapping[Control, tuple[str, ...]]
    """Playwright key names a game must accept for each control it supports. Bots press the
    first one."""

    directions: Mapping[Control, tuple[float, float, float]]
    """World-space ``(x, y, z)`` unit direction of each movement control, so bots can steer
    from ``getState()`` positions alone."""

    smoke_move: Control
    """Control the smoke test holds to check that the player moves."""


CONTROLS_2D: Final = ControlScheme(
    bindings=MappingProxyType(
        {
            Control.LEFT: ("ArrowLeft", "KeyA"),
            Control.RIGHT: ("ArrowRight", "KeyD"),
            Control.JUMP: ("Space", "ArrowUp", "KeyW"),
            Control.RESPAWN: ("KeyR",),
        }
    ),
    directions=MappingProxyType(
        {
            Control.LEFT: (-1.0, 0.0, 0.0),
            Control.RIGHT: (1.0, 0.0, 0.0),
        }
    ),
    smoke_move=Control.RIGHT,
)
"""Side view: run left and right along x; Space, Up or W jump; R respawns."""
