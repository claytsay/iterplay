"""[3] Playtest runner (HLD §4 [3])."""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from iterplay.bots.base import Bot
from iterplay.bots.registry import BOT_TYPES
from iterplay.contract.controls import ControlScheme
from iterplay.contract.state import HARNESS_GLOBAL
from iterplay.contract.url import GameUrlParams
from iterplay.errors import ContractError
from iterplay.harness.browser import GameSession, HarnessBrowser, game_url
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport, RunEnd
from iterplay.models.telemetry import TelemetryRecord
from iterplay.storage.artifacts import write_model, write_text
from iterplay.storage.layout import VersionPaths
from iterplay.storage.telemetry import TelemetryWriter

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlaytestConfig:
    """Raises ValueError for a repeated bot, a non-positive duration or rate, or a
    concurrency below 1."""

    bots: tuple[BotName, ...] = (BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE)
    max_duration_s: float = 60.0

    tick_hz: float = 10.0
    """Rate of snapshots, telemetry lines and bot decisions."""

    max_concurrency: int = 3
    """Browser sessions running at once; three is the laptop limit."""

    ready_timeout_s: float = 10.0
    """How long a session waits, from starting to load the page, for
    ``window.iterplay.ready``. A game that never becomes ready counts as crashed."""

    def __post_init__(self) -> None:
        if len(set(self.bots)) != len(self.bots):
            raise ValueError(f"bots must not repeat, got {[str(bot) for bot in self.bots]}")
        for name in ("max_duration_s", "tick_hz", "ready_timeout_s"):
            value = getattr(self, name)
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.max_concurrency < 1:
            raise ValueError(f"max_concurrency must be at least 1, got {self.max_concurrency}")


class PlaytestRunner:
    """Runs every bot in parallel, each in its own recorded browser context.

    A run lasts at most ``max_duration_s`` and ends early once ``levelComplete`` is true.
    Within a run, a single tick loop owns ``getState()``: it writes each snapshot to telemetry
    and hands the same snapshot to the bot, because reading a snapshot drains its events.
    Bots and sessions share the game template's control scheme.

    Ticks fall on a fixed grid of ``video_t`` (``1 / tick_hz`` apart from the first tick), so
    slow ticks never make the rate drift; a tick that overruns skips the grid points it
    missed. Each bot plays ``index.html?seed=<game seed>&fixedStep=1&harness=1&bot=<name>``.
    A session that fails, because the game broke the contract, crashed or hung, or because
    of a harness fault, ends as :attr:`~iterplay.models.playtest.RunEnd.CRASHED` with a
    ``crash_detail``; it never stops the other bots.
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
        ``paths.playtest_dir``. A bot missing from ``bot_seeds`` gets seed 0. ``console.log``
        holds every session's console lines in bot order, each prefixed with ``[<bot>] ``.
        """
        limit = asyncio.Semaphore(self._config.max_concurrency)
        consoles: dict[BotName, list[str]] = {name: [] for name in self._config.bots}

        async def play(name: BotName) -> BotRunResult:
            seed = bot_seeds.get(name, 0)
            params = GameUrlParams(
                seed=game_seed, fixed_step=True, harness_hud=True, bot=name.value
            )
            async with limit:
                bot = BOT_TYPES[name](seed, self._controls)
                url = game_url(base_url, params)
                return await self._run_bot(bot, url, paths, bot_seed=seed, console=consoles[name])

        runs = await asyncio.gather(*(play(name) for name in self._config.bots))
        report = PlaytestReport(game_seed=game_seed, runs=list(runs))
        write_text(paths.console_log, _console_log(consoles))
        write_model(paths.playtest_report, report)
        return report

    async def _run_bot(
        self,
        bot: Bot,
        url: str,
        paths: VersionPaths,
        *,
        bot_seed: int = 0,
        console: list[str] | None = None,
    ) -> BotRunResult:
        """Play one recorded session, writing its video and telemetry; failures are reported
        as a crashed run, never raised.

        ``bot_seed`` is recorded in the result, and the session's console lines are appended
        to ``console``.
        """
        name = bot.name
        result = BotRunResult(
            bot=name,
            bot_seed=bot_seed,
            ended_by=RunEnd.CRASHED,
            duration_s=0.0,
            crash_detail="The session never started.",
        )
        try:
            video = paths.raw_video(name)
            video.unlink(missing_ok=True)
            with TelemetryWriter(paths.telemetry(name)) as telemetry:
                async with self._browser.session(self._controls, record_video_to=video) as session:
                    ended_by, detail = await self._attempt(session, bot, url, telemetry)
                    result = BotRunResult(
                        bot=name,
                        bot_seed=bot_seed,
                        ended_by=ended_by,
                        duration_s=min(session.video_t(), self._config.max_duration_s),
                        uncaught_errors=list(session.uncaught_errors),
                        console_errors=list(session.console_errors),
                        crash_detail=detail,
                    )
                    with suppress(ContractError):
                        await session.release_all()
                    if console is not None:
                        console.extend(session.console_lines)
        except Exception as exc:
            _log.exception("The %s session could not be recorded", name)
            crash = {"ended_by": RunEnd.CRASHED, "crash_detail": f"The harness failed: {exc!r}"}
            result = result.model_copy(update=crash)
        return result

    async def _attempt(
        self, session: GameSession, bot: Bot, url: str, telemetry: TelemetryWriter
    ) -> tuple[RunEnd, str | None]:
        """:meth:`_play`, with any failure turned into a crashed run and its detail."""
        try:
            return await self._play(session, bot, url, telemetry)
        except ContractError as exc:
            return RunEnd.CRASHED, str(exc)
        except Exception as exc:
            _log.exception("The %s session failed", bot.name)
            return RunEnd.CRASHED, f"The harness failed: {exc!r}"

    async def _play(
        self, session: GameSession, bot: Bot, url: str, telemetry: TelemetryWriter
    ) -> tuple[RunEnd, str | None]:
        """Load the game, wait for it to be ready, then run the tick loop until the run ends.

        Raises:
            ContractError: The game failed to load, or a snapshot or key press failed.
        """
        config = self._config
        started = session.video_t()
        await session.open(url, timeout_s=config.ready_timeout_s)
        remaining = config.ready_timeout_s - (session.video_t() - started)
        if not await session.wait_until_ready(max(remaining, 0.0)):
            detail = (
                f"window.{HARNESS_GLOBAL}.ready did not become true within "
                f"{config.ready_timeout_s:g} s"
            )
            return RunEnd.CRASHED, detail
        period = 1 / config.tick_hz
        first = session.video_t()
        tick = 0
        while True:
            video_t = session.video_t()
            state = await session.get_state()
            telemetry.append(TelemetryRecord(video_t=video_t, state=state))
            if state.level_complete:
                return RunEnd.LEVEL_COMPLETE, None
            if video_t >= config.max_duration_s:
                return RunEnd.TIME_LIMIT, None
            await session.hold(bot.decide(state, video_t))
            tick = max(tick + 1, math.ceil((session.video_t() - first) / period))
            await asyncio.sleep(max(0.0, first + tick * period - session.video_t()))


def _console_log(consoles: Mapping[BotName, Sequence[str]]) -> str:
    """Every session's console lines, each line of a message prefixed with its bot's name."""
    return "".join(
        f"[{bot}] {line}\n"
        for bot, messages in consoles.items()
        for message in messages
        for line in message.splitlines() or [""]
    )
