"""The playtest runner (HLD §4 [3]): one recorded session per bot, with telemetry, the console
log and the report, against the simple fixture game and the hand-written reference game."""

import itertools
import shutil
import statistics
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import pytest
from playwright.async_api import Page

from iterplay.bots.base import Bot
from iterplay.bots.registry import BOT_TYPES
from iterplay.bots.speedrunner import SpeedrunnerBot
from iterplay.contract.controls import CONTROLS_2D, Control, ControlScheme
from iterplay.contract.state import GameEventType, GameState
from iterplay.harness import browser as browser_module
from iterplay.harness import playtest as playtest_module
from iterplay.harness.browser import BrowserOptions, GameSession, HarnessBrowser
from iterplay.harness.playtest import PlaytestConfig, PlaytestRunner
from iterplay.harness.server import game_server
from iterplay.models.playtest import BotName, PlaytestReport, RunEnd
from iterplay.models.telemetry import TelemetryRecord
from iterplay.storage.artifacts import read_model
from iterplay.storage.layout import VersionPaths
from iterplay.storage.telemetry import read_telemetry

pytestmark = pytest.mark.browser

FIXTURES = Path(__file__).parent / "fixtures" / "games"
REFERENCE_GAME = Path(__file__).parents[1] / "examples" / "reference_game"
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
ALL_BOTS = (BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE)
SEEDS = MappingProxyType({BotName.RANDOM: 11, BotName.SPEEDRUNNER: 12})


@pytest.fixture
async def browser() -> AsyncIterator[HarnessBrowser]:
    async with HarnessBrowser(BrowserOptions()) as harness:
        yield harness


@pytest.fixture
def paths(tmp_path: Path) -> VersionPaths:
    return VersionPaths(tmp_path / "v1")


async def playtest(
    browser: HarnessBrowser,
    game_dir: Path,
    paths: VersionPaths,
    config: PlaytestConfig,
    *,
    controls: ControlScheme = CONTROLS_2D,
) -> PlaytestReport:
    with game_server(game_dir, port=0) as server:
        runner = PlaytestRunner(browser, controls, config)
        return await runner.run(server.base_url, game_seed=5, bot_seeds=SEEDS, paths=paths)


def simple_with(directory: Path, script: str) -> Path:
    """A copy of the simple game with ``script`` appended to its ``main.js``."""
    game = shutil.copytree(FIXTURES / "simple", directory)
    with (game / "main.js").open("a") as main:
        main.write(script)
    return game


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def assert_recorded(paths: VersionPaths, bot: BotName) -> list[TelemetryRecord]:
    """Check the bot's video is a WebM file and return its telemetry."""
    assert paths.raw_video(bot).read_bytes()[:4] == WEBM_MAGIC
    assert paths.raw_video(bot).stat().st_size > 1000
    return read_telemetry(paths.telemetry(bot))


class CountingBrowser(HarnessBrowser):
    """Counts the sessions open at once."""

    def __init__(self, options: BrowserOptions) -> None:
        super().__init__(options)
        self.open_sessions = 0
        self.peak = 0

    def session(
        self, controls: ControlScheme, *, record_video_to: Path | None = None
    ) -> AbstractAsyncContextManager[GameSession]:
        return self._counted(super().session(controls, record_video_to=record_video_to))

    @asynccontextmanager
    async def _counted(
        self, inner: AbstractAsyncContextManager[GameSession]
    ) -> AsyncIterator[GameSession]:
        async with inner as session:
            self.open_sessions += 1
            self.peak = max(self.peak, self.open_sessions)
            try:
                yield session
            finally:
                self.open_sessions -= 1


class RecordingSpeedrunner(SpeedrunnerBot):
    """A speedrunner that remembers every snapshot it was given."""

    seen: ClassVar[list[tuple[float, GameState]]] = []

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        self.seen.append((video_t, state))
        return super().decide(state, video_t)


class ExplodingBot(Bot):
    """Plays for three ticks, then fails like a buggy bot."""

    name = BotName.RANDOM

    def __init__(self, seed: int, controls: ControlScheme) -> None:
        super().__init__(seed, controls)
        self._ticks = 0

    def decide(self, state: GameState, video_t: float) -> frozenset[Control]:
        self._ticks += 1
        if self._ticks > 3:
            raise RuntimeError("bot exploded")
        return frozenset({Control.RIGHT})


def bots(**replacements: type[Bot]) -> Mapping[BotName, type[Bot]]:
    """``BOT_TYPES`` with some personas replaced, by bot name."""
    return MappingProxyType({**BOT_TYPES, **{BotName(k): v for k, v in replacements.items()}})


# A full playtest


async def test_a_playtest_records_every_bot(browser: HarnessBrowser, paths: VersionPaths) -> None:
    report = await playtest(browser, FIXTURES / "simple", paths, PlaytestConfig(max_duration_s=6))

    assert read_model(paths.playtest_report, PlaytestReport) == report
    assert report.game_seed == 5
    assert [(run.bot, run.bot_seed) for run in report.runs] == [
        (BotName.RANDOM, 11),
        (BotName.SPEEDRUNNER, 12),
        (BotName.NEWBIE, 0),
    ]
    for run in report.runs:
        records = assert_recorded(paths, run.bot)
        times = [record.video_t for record in records]
        assert times == sorted(set(times))
        assert 0 < times[0] < 3
        assert run.ended_by in {RunEnd.LEVEL_COMPLETE, RunEnd.TIME_LIMIT}
        assert 0 < run.duration_s <= 6
        assert (run.uncaught_errors, run.console_errors, run.crash_detail) == ([], [], None)
        if run.ended_by is RunEnd.TIME_LIMIT:
            assert run.duration_s == 6
            assert times[-1] >= 6
            assert all(time < 6 for time in times[:-1])
    assert report.runs[1].ended_by is RunEnd.LEVEL_COMPLETE
    assert paths.console_log.read_text() == ""
    assert names(paths.playtest_dir) == sorted(
        [
            "console.log",
            "report.json",
            *(f"{bot}.webm" for bot in ALL_BOTS),
            *(f"{bot}.telemetry.jsonl" for bot in ALL_BOTS),
        ]
    )


async def test_the_speedrunner_completes_the_simple_game(
    browser: HarnessBrowser, paths: VersionPaths
) -> None:
    config = PlaytestConfig(bots=(BotName.SPEEDRUNNER,), max_duration_s=6)

    (run,) = (await playtest(browser, FIXTURES / "simple", paths, config)).runs

    records = assert_recorded(paths, BotName.SPEEDRUNNER)
    final = records[-1].state
    assert run.ended_by is RunEnd.LEVEL_COMPLETE
    assert run.duration_s < 6
    assert final.level_complete
    assert all(not record.state.level_complete for record in records[:-1])
    assert [item.id for item in final.collectibles if item.collected] == ["g1", "g2"]
    events = [event.type for record in records for event in record.state.events]
    assert events == [GameEventType.COLLECT, GameEventType.COLLECT, GameEventType.COMPLETE]


async def test_each_bot_gets_exactly_the_snapshots_that_were_recorded(
    browser: HarnessBrowser, paths: VersionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playtest_module, "BOT_TYPES", bots(speedrunner=RecordingSpeedrunner))
    monkeypatch.setattr(RecordingSpeedrunner, "seen", [])
    config = PlaytestConfig(bots=(BotName.SPEEDRUNNER,), max_duration_s=6)

    await playtest(browser, FIXTURES / "simple", paths, config)

    records = read_telemetry(paths.telemetry(BotName.SPEEDRUNNER))
    assert RecordingSpeedrunner.seen == [(record.video_t, record.state) for record in records[:-1]]


async def test_ticks_fall_on_a_fixed_grid(browser: HarnessBrowser, paths: VersionPaths) -> None:
    config = PlaytestConfig(bots=(BotName.RANDOM,), max_duration_s=2, tick_hz=20)

    await playtest(browser, FIXTURES / "frozen", paths, config)

    times = [record.video_t for record in read_telemetry(paths.telemetry(BotName.RANDOM))]
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    assert statistics.median(gaps) == pytest.approx(0.05, abs=0.01)
    assert min(gaps) > 0.025
    offsets = [(time - times[0]) / 0.05 for time in times]
    assert all(abs(offset - round(offset)) * 0.05 < 0.02 for offset in offsets)


async def test_the_time_limit_ends_a_run(browser: HarnessBrowser, paths: VersionPaths) -> None:
    config = PlaytestConfig(bots=(BotName.SPEEDRUNNER,), max_duration_s=1)

    (run,) = (await playtest(browser, FIXTURES / "frozen", paths, config)).runs

    times = [record.video_t for record in assert_recorded(paths, BotName.SPEEDRUNNER)]
    assert run.ended_by is RunEnd.TIME_LIMIT
    assert run.duration_s == 1
    assert times[-1] >= 1 > times[-2]


# Concurrency and isolation


@pytest.mark.parametrize("cap", [1, 2, 3])
async def test_the_concurrency_cap_is_respected(paths: VersionPaths, cap: int) -> None:
    config = PlaytestConfig(max_duration_s=1.5, max_concurrency=cap)
    async with CountingBrowser(BrowserOptions()) as browser:
        report = await playtest(browser, FIXTURES / "frozen", paths, config)

    assert browser.peak == cap
    assert [run.ended_by for run in report.runs] == [RunEnd.TIME_LIMIT] * 3


async def test_a_crashing_bot_does_not_stop_the_others(
    browser: HarnessBrowser, paths: VersionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playtest_module, "BOT_TYPES", bots(random=ExplodingBot))
    config = PlaytestConfig(max_duration_s=6)

    random, speedrunner, newbie = (await playtest(browser, FIXTURES / "simple", paths, config)).runs

    assert random.ended_by is RunEnd.CRASHED
    assert random.crash_detail == "The harness failed: RuntimeError('bot exploded')"
    assert len(assert_recorded(paths, BotName.RANDOM)) == 4
    assert speedrunner.ended_by is RunEnd.LEVEL_COMPLETE
    assert newbie.ended_by is not RunEnd.CRASHED
    assert read_model(paths.playtest_report, PlaytestReport).runs[0] == random


@pytest.mark.parametrize(
    ("bot", "fault", "detail"),
    [
        (
            BotName.RANDOM,
            "() => (game.t > 0.5 ? { broken: true } : getState())",
            "getState() returned an invalid snapshot: t: Field required; player: Field "
            "required; goal: Field required (and 5 more)",
        ),
        (
            BotName.NEWBIE,
            "() => { while (game.t > 0.5) {} return getState(); }",
            "getState() did not return within 1 s; the page may be stuck in an endless loop",
        ),
    ],
    ids=["broken-state", "hang"],
)
async def test_a_session_whose_game_breaks_crashes_alone(
    browser: HarnessBrowser,
    paths: VersionPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bot: BotName,
    fault: str,
    detail: str,
) -> None:
    monkeypatch.setattr(browser_module, "CALL_TIMEOUT_S", 1.0)
    game = simple_with(
        tmp_path / "game",
        f"""
if (options.bot === "{bot}") {{
  const getState = window.iterplay.getState;
  window.iterplay.getState = {fault};
}}
""",
    )

    report = await playtest(browser, game, paths, PlaytestConfig(max_duration_s=6))

    runs = {run.bot: run for run in report.runs}
    assert runs[bot].ended_by is RunEnd.CRASHED
    assert runs[bot].crash_detail == detail
    assert assert_recorded(paths, bot)
    assert runs[BotName.SPEEDRUNNER].ended_by is RunEnd.LEVEL_COMPLETE
    assert all(run.ended_by is not RunEnd.CRASHED for name, run in runs.items() if name != bot)


@pytest.mark.parametrize(
    ("fixture", "detail"),
    [
        ("no_api", "window.iterplay.ready did not become true within 1 s"),
        ("bad_state", "getState() returned an invalid snapshot: t: Input should be a valid number"),
    ],
)
async def test_a_game_that_breaks_the_contract_crashes_every_run(
    browser: HarnessBrowser, paths: VersionPaths, fixture: str, detail: str
) -> None:
    paths.playtest_dir.mkdir(parents=True)
    for bot in ALL_BOTS:
        paths.raw_video(bot).write_bytes(b"a video from an older playtest")
        paths.telemetry(bot).write_text("not telemetry\n")
    config = PlaytestConfig(max_duration_s=6, ready_timeout_s=1)

    report = await playtest(browser, FIXTURES / fixture, paths, config)

    for run in report.runs:
        assert run.ended_by is RunEnd.CRASHED
        assert run.crash_detail is not None
        assert run.crash_detail.startswith(detail)
        assert run.duration_s < 4
        assert assert_recorded(paths, run.bot) == []
    assert read_model(paths.playtest_report, PlaytestReport) == report


async def test_a_session_whose_video_cannot_be_kept_crashes_alone(
    browser: HarnessBrowser, paths: VersionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_video = browser_module._keep_video

    async def disk_full_for_random(page: Page, target: Path) -> None:
        if target == paths.raw_video(BotName.RANDOM):
            raise OSError("No space left on device")
        await keep_video(page, target)

    monkeypatch.setattr(browser_module, "_keep_video", disk_full_for_random)
    config = PlaytestConfig(max_duration_s=1.5)

    random, *others = (await playtest(browser, FIXTURES / "throws", paths, config)).runs

    assert random.ended_by is RunEnd.CRASHED
    assert random.crash_detail == "The harness failed: OSError('No space left on device')"
    assert random.duration_s == 1.5
    assert random.uncaught_errors
    assert not paths.raw_video(BotName.RANDOM).exists()
    assert read_telemetry(paths.telemetry(BotName.RANDOM))
    assert [run.ended_by for run in others] == [RunEnd.TIME_LIMIT, RunEnd.TIME_LIMIT]
    for run in others:
        assert_recorded(paths, run.bot)


# Errors and the console log


async def test_errors_are_reported_per_bot_and_logged(
    browser: HarnessBrowser, paths: VersionPaths
) -> None:
    config = PlaytestConfig(bots=(BotName.NEWBIE, BotName.RANDOM), max_duration_s=1.5)

    report = await playtest(browser, FIXTURES / "throws", paths, config)

    error = "TypeError: Cannot spawn the boss: its level data is missing"
    for run in report.runs:
        assert run.ended_by is RunEnd.TIME_LIMIT
        assert run.uncaught_errors == [f"{error}\n    at spawnBoss (main.js:39:9)"]
    assert paths.console_log.read_text() == (
        f"[newbie] [uncaught] {error}\n"
        "[newbie]     at spawnBoss (main.js:39:9)\n"
        f"[random] [uncaught] {error}\n"
        "[random]     at spawnBoss (main.js:39:9)\n"
    )


async def test_the_console_log_prefixes_every_line_with_its_bot(
    browser: HarnessBrowser, paths: VersionPaths, tmp_path: Path
) -> None:
    game = simple_with(
        tmp_path / "game",
        'console.log(`${options.bot} started\\nsecond line`);\nconsole.warn("careful");\n',
    )
    config = PlaytestConfig(max_duration_s=1)

    await playtest(browser, game, paths, config)

    assert paths.console_log.read_text() == "".join(
        f"[{bot}] [log] {bot} started\n[{bot}] second line\n[{bot}] [warning] careful\n"
        for bot in ALL_BOTS
    )


# Configuration


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"bots": (BotName.RANDOM, BotName.RANDOM)}, "bots must not repeat"),
        ({"max_duration_s": 0.0}, "max_duration_s must be positive"),
        ({"tick_hz": 0.0}, "tick_hz must be positive"),
        ({"tick_hz": float("nan")}, "tick_hz must be positive"),
        ({"ready_timeout_s": -1.0}, "ready_timeout_s must be positive"),
        ({"max_concurrency": 0}, "max_concurrency must be at least 1"),
    ],
)
def test_the_config_is_validated(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PlaytestConfig(**config)  # type: ignore[arg-type]


# The reference game


@pytest.mark.slow
async def test_the_speedrunner_completes_the_reference_game(
    browser: HarnessBrowser, paths: VersionPaths, tmp_path: Path
) -> None:
    game = shutil.copytree(REFERENCE_GAME, tmp_path / "game")
    config = PlaytestConfig(bots=(BotName.SPEEDRUNNER,), max_duration_s=60)

    (run,) = (await playtest(browser, game, paths, config)).runs

    final = assert_recorded(paths, BotName.SPEEDRUNNER)[-1].state
    assert run.ended_by is RunEnd.LEVEL_COMPLETE
    assert run.duration_s < 60
    assert final.level_complete
    assert run.uncaught_errors == []
