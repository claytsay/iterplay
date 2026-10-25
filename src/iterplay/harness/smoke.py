"""[2] Smoke test, so a playtest is never wasted on a broken build (HLD §4 [2])."""

from dataclasses import dataclass

from iterplay.contract.controls import ControlScheme
from iterplay.harness.browser import HarnessBrowser
from iterplay.models.smoke import SmokeReport


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    seed: int = 1
    ready_timeout_s: float = 10.0
    move_hold_s: float = 3.0


class SmokeTester:
    """Loads ``index.html?seed=1&fixedStep=1`` in a headless browser and runs every
    :class:`~iterplay.models.smoke.SmokeCheck`.

    The movement check holds ``controls.smoke_move`` for ``move_hold_s`` seconds.
    """

    def __init__(
        self,
        browser: HarnessBrowser,
        controls: ControlScheme,
        config: SmokeConfig | None = None,
    ) -> None:
        self._browser = browser
        self._controls = controls
        self._config = config or SmokeConfig()

    async def run(self, base_url: str, *, attempt: int = 1) -> SmokeReport:
        """Smoke-test the game served at ``base_url``. Failures are reported, never raised."""
        raise NotImplementedError
