"""The ``window.iterplay`` harness API (HLD §3.3).

Every game must expose::

    window.iterplay = {
      version: "1",
      ready: false,        // true once the scene is loaded and playable
      getState() { ... },  // returns a GameState; polled about 10 times per second
      reset(seed) { ... }, // restarts the level from scratch
    };

The models below are the single source of truth for ``getState()``: the smoke test validates
against them, the playtest runner parses every snapshot with them, and their JSON schema is
embedded in the Builder and Fixer prompts.

One schema serves every game template. Positions are 3D: 2D templates play in the x/y plane
(x right, y up) and may omit ``z`` and ``vz``, which default to 0.
"""

from enum import StrEnum
from typing import Final

from pydantic import ConfigDict, Field

from iterplay.models.base import TolerantModel

HARNESS_GLOBAL: Final = "iterplay"
"""Name of the global the harness reads (``window.iterplay``)."""

HARNESS_API_VERSION: Final = "1"
"""Value games must expose as ``window.iterplay.version``."""

KILL_PLANE_Y: Final = -20.0
"""Height below which the player is out of bounds: games emit ``oob`` and respawn the player."""


class GameEventType(StrEnum):
    DEATH = "death"
    COLLECT = "collect"
    RESPAWN = "respawn"
    COMPLETE = "complete"
    OOB = "oob"


class GameEvent(TolerantModel):
    """A discrete event that happened since the previous ``getState()`` call.

    Event-specific extra fields are preserved rather than dropped.
    """

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    type: GameEventType
    t: float = Field(ge=0, description="Game time of the event, in seconds.")
    id: str | None = Field(default=None, description="Collectible id, for collect events.")
    intentional: bool = Field(
        default=False,
        description="For oob events: the player fell into a pit the level design intends.",
    )


class Vec3(TolerantModel):
    """A point in world space. +y is up; 2D games leave z at 0."""

    x: float
    y: float
    z: float = 0.0


class PlayerState(TolerantModel):
    """Player position, velocity, and whether they are standing on something."""

    x: float
    y: float
    z: float = 0.0
    vx: float
    vy: float
    vz: float = 0.0
    grounded: bool


class Collectible(TolerantModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: str
    x: float
    y: float
    z: float = 0.0
    collected: bool


class GameState(TolerantModel):
    """One ``getState()`` snapshot. Field names match the JavaScript contract on the wire."""

    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    t: float = Field(ge=0, description="Seconds of game time.")
    player: PlayerState
    goal: Vec3
    collectibles: list[Collectible]
    deaths: int = Field(ge=0)
    level_complete: bool = Field(alias="levelComplete")
    fps: float = Field(ge=0)
    events: list[GameEvent] = Field(
        description="Events since the previous call; reading the state drains them."
    )
