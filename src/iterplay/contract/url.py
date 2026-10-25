"""URL parameters every generated game must honour (HLD §3.2)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GameUrlParams:
    """Query parameters the harness appends to ``index.html``."""

    seed: int
    """``?seed=<int>``: all in-game randomness comes from a PRNG seeded with this value."""

    fixed_step: bool = True
    """``?fixedStep=1``: advance the simulation exactly 1/60 s per frame."""

    harness_hud: bool = True
    """``?harness=1``: show the clock and bot-name HUD overlay for the video critic."""

    bot: str | None = None
    """``?bot=<name>``: the bot name the HUD shows."""

    def to_query(self) -> str:
        """Encode as a query string, e.g. ``seed=7&fixedStep=1&harness=1&bot=newbie``.

        Disabled flags are omitted rather than sent as ``0``.
        """
        raise NotImplementedError
