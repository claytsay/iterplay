"""[3] Playtest runner (HLD §4 [3])."""

from collections.abc import Mapping
from dataclasses import dataclass

from iterplay.bots.base import Bot
from iterplay.contract.controls import ControlScheme
from iterplay.harness.browser import HarnessBrowser
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport
from iterplay.storage.layout import VersionPaths


@dataclass(frozen=True, slots=True)
class PlaytestConfig:
    bots: tuple[BotName, ...] = (BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE)
    max_duration_s: float = 60.0

    tick_hz: float = 10.0
    """Rate of snapshots, telemetry lines and bot decisions."""

    max_concurrency: int = 3
    """Browser sessions running at once; three is the laptop limit."""


class PlaytestRunner:
    """Runs every bot in parallel, each in its own recorded browser context.

    A run lasts at most ``max_duration_s`` and ends early once ``levelComplete`` is true.
    Within a run, a single tick loop owns ``getState()``: it writes each snapshot to telemetry
    and hands the same snapshot to the bot, because reading a snapshot drains its events.
    Bots and sessions share the game template's control scheme.
    """

    def __init__(
        self,
        browser: HarnessBrowser,
        controls: ControlScheme,
        config: PlaytestConfig | None = None,
    ) -> None:
        self._browser = browser
        self._controls = controls
        self._config = config or PlaytestConfig()

    async def run(
        self,
        base_url: str,
        *,
        game_seed: int,
        bot_seeds: Mapping[BotName, int],
        paths: VersionPaths,
    ) -> PlaytestReport:
        """Play the game at ``base_url`` with every configured bot.

        Writes each bot's video and telemetry, ``console.log`` and ``report.json`` under
        ``paths.playtest_dir``.
        """
        raise NotImplementedError

    async def _run_bot(self, bot: Bot, url: str, paths: VersionPaths) -> BotRunResult:
        raise NotImplementedError
