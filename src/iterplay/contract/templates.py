"""Game templates: the kind of game iterplay builds (HLD §1 calls this the genre template).

A template bundles everything that depends on the kind of game: its control scheme, its
dimensionality, how prompts describe it, and its contract and style-guide prompts in
``agents/prompts/<template id>/``. The rest of the pipeline, from telemetry to the Verifier,
Scorer and media prep, is template-agnostic. Positions are 3D everywhere, and 2D games keep
z at 0.

Only the 2D platformer is supported for now. Adding a 3D template takes a ``ControlScheme``
with ``FORWARD`` and ``BACK``, prompts in ``agents/prompts/<template id>/``, an entry in
:data:`TEMPLATES`, and a hand-written reference level to tune the bots on.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from iterplay.contract.controls import CONTROLS_2D, ControlScheme


class TemplateId(StrEnum):
    PLATFORMER_2D = "platformer-2d"


@dataclass(frozen=True, slots=True)
class GameTemplate:
    id: TemplateId

    description: str
    """How prompts name the game, e.g. "single-level 2D platformer"."""

    dimensions: Literal[2, 3]
    """2: the level lies in the x/y plane (x right, y up) and z stays 0. 3: all three axes."""

    controls: ControlScheme


TEMPLATES: Final[Mapping[TemplateId, GameTemplate]] = MappingProxyType(
    {
        TemplateId.PLATFORMER_2D: GameTemplate(
            id=TemplateId.PLATFORMER_2D,
            description="single-level 2D platformer",
            dimensions=2,
            controls=CONTROLS_2D,
        ),
    }
)

DEFAULT_TEMPLATE: Final = TemplateId.PLATFORMER_2D
