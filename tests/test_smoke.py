"""The smoke test (HLD §4 [2]) against fixture games that pass, or break exactly one part of
the contract, and against the hand-written reference game rendered with WebGL."""

import logging
import shutil
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest

from iterplay.contract.controls import CONTROLS_2D, Control, ControlScheme
from iterplay.harness import browser as browser_module
from iterplay.harness.browser import BLOCKED_REQUEST, BrowserOptions, HarnessBrowser
from iterplay.harness.server import game_server
from iterplay.harness.smoke import SmokeConfig, SmokeTester
from iterplay.models.smoke import SmokeCheck, SmokeReport
from iterplay.storage.artifacts import read_model, write_model

pytestmark = pytest.mark.browser

FIXTURES = Path(__file__).parent / "fixtures" / "games"
REFERENCE_GAME = Path(__file__).parents[1] / "examples" / "reference_game"
QUICK = SmokeConfig(ready_timeout_s=2.0, move_hold_s=1.0)

READY = SmokeCheck.READY
STATE_SCHEMA = SmokeCheck.STATE_SCHEMA
NO_ERRORS = SmokeCheck.NO_ERRORS
PLAYER_MOVES = SmokeCheck.PLAYER_MOVES

MINIMAL_GAME = """
const held = new Set();
window.addEventListener("keydown", (event) => held.add(event.code));
window.addEventListener("keyup", (event) => held.delete(event.code));
let t = 0;
let x = 0;
let deaths = 0;
let readyAt = 0;
window.iterplay = {
  version: "1",
  ready: false,
  getState: () => ({
    t, player: { x, y: 0.5, vx: 0, vy: 0, grounded: true }, goal: { x: 12, y: 0.5 },
    collectibles: [], deaths, levelComplete: false, fps: 60, events: [],
  }),
  reset() {},
};
function frame() {
  t += 1 / 60;
  step();
  window.iterplay.ready = t >= readyAt;
  window.requestAnimationFrame(frame);
}
window.requestAnimationFrame(frame);
"""
"""A game reduced to the harness API. Append a ``function step() { ... }`` that moves ``x``;
appended code may also set ``readyAt``, the game time at which the game becomes ready."""


@pytest.fixture
async def browser() -> AsyncIterator[HarnessBrowser]:
    async with HarnessBrowser(BrowserOptions()) as harness:
        yield harness


async def smoke(
    browser: HarnessBrowser,
    game_dir: Path,
    config: SmokeConfig | None = None,
    *,
    controls: ControlScheme = CONTROLS_2D,
    attempt: int = 1,
) -> SmokeReport:
    with game_server(game_dir, port=0) as server:
        return await SmokeTester(browser, controls, config).run(server.base_url, attempt=attempt)


def minimal_game(directory: Path, step: str) -> Path:
    """:data:`MINIMAL_GAME` with ``step`` as its per-frame update."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(
        '<!doctype html><link rel="icon" href="data:,">'
        '<script type="module" src="./main.js"></script>\n'
    )
    (directory / "main.js").write_text(MINIMAL_GAME + step)
    return directory


def failed(report: SmokeReport) -> set[SmokeCheck]:
    return {result.check for result in report.checks if not result.passed}


def details(report: SmokeReport) -> dict[SmokeCheck, str]:
    return {result.check: result.detail for result in report.checks}


# Games that pass


async def test_the_simple_game_passes_every_check(browser: HarnessBrowser) -> None:
    report = await smoke(browser, FIXTURES / "simple")

    assert report.passed
    assert [result.check for result in report.checks] == list(SmokeCheck)
    found = details(report)
    assert found[READY].startswith("window.iterplay.ready became true after ")
    assert found[STATE_SCHEMA] == (
        "getState() returned a valid snapshot: the player is at (0.00, 0.50), "
        "the goal at (12.00, 0.50), with 2 collectibles."
    )
    assert found[NO_ERRORS] == "No uncaught errors, console errors or blocked requests."
    assert found[PLAYER_MOVES].startswith("The player moved from (0.00, 0.50) to (")
    assert found[PLAYER_MOVES].endswith(") while right (ArrowRight) was held.")
    assert (report.uncaught_errors, report.console_errors, report.attempt) == ([], [], 1)


async def test_the_reference_game_passes_with_webgl_and_the_vendored_three(
    browser: HarnessBrowser, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")
    game = shutil.copytree(REFERENCE_GAME, tmp_path / "game")

    report = await smoke(browser, game)

    assert report.passed, report.checks
    requests = [record.getMessage() for record in caplog.records]
    assert any('"GET /vendor/three.module.js HTTP/1.1" 200' in line for line in requests)
    assert details(report)[STATE_SCHEMA].endswith("with 10 collectibles.")


async def test_the_page_is_loaded_with_the_seed_and_a_fixed_step(
    browser: HarnessBrowser, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")

    await smoke(browser, FIXTURES / "simple", SmokeConfig(seed=7, move_hold_s=0.3))

    pages = [
        record.getMessage() for record in caplog.records if "/index.html" in record.getMessage()
    ]
    assert len(pages) == 1
    assert '"GET /index.html?seed=7&fixedStep=1 HTTP/1.1" 200' in pages[0]


async def test_the_report_keeps_the_attempt_and_round_trips(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    report = await smoke(browser, FIXTURES / "frozen", QUICK, attempt=3)
    write_model(tmp_path / "smoke.json", report)

    assert report.attempt == 3
    assert read_model(tmp_path / "smoke.json", SmokeReport) == report


# Games that break one part of the contract


BROKEN: Mapping[str, tuple[set[SmokeCheck], dict[SmokeCheck, str]]] = {
    "no_api": (
        {READY, STATE_SCHEMA, PLAYER_MOVES},
        {
            READY: "window.iterplay.ready did not become true within 2 s: "
            "window.iterplay is not defined",
            STATE_SCHEMA: "skipped: the game never became ready.",
            PLAYER_MOVES: "skipped: the game never became ready.",
        },
    ),
    "throws": ({NO_ERRORS}, {NO_ERRORS: "1 uncaught error."}),
    "cdn": ({NO_ERRORS}, {}),
    "frozen": (
        {PLAYER_MOVES},
        {
            PLAYER_MOVES: "The player stayed at (0.00, 0.50) while right (ArrowRight) was held "
            "for 1 s (10 getState() samples)."
        },
    ),
    "bad_state": (
        {STATE_SCHEMA, PLAYER_MOVES},
        {
            STATE_SCHEMA: "getState() returned an invalid snapshot: t: Input should be a valid "
            "number, unable to parse string as a number; player.y: Field required; "
            "player.vx: Field required (and 6 more)",
            PLAYER_MOVES: "skipped: getState() did not return a valid snapshot.",
        },
    ),
}


@pytest.mark.parametrize("name", list(BROKEN))
async def test_a_broken_game_fails_exactly_its_checks(browser: HarnessBrowser, name: str) -> None:
    expected_failures, expected_details = BROKEN[name]

    report = await smoke(browser, FIXTURES / name, QUICK)

    assert not report.passed
    assert failed(report) == expected_failures
    assert [result.check for result in report.checks] == list(SmokeCheck)
    found = details(report)
    for check, detail in expected_details.items():
        assert found[check] == detail
    assert all(found[check] for check in SmokeCheck)


async def test_uncaught_errors_are_reported(browser: HarnessBrowser) -> None:
    report = await smoke(browser, FIXTURES / "throws", QUICK)

    assert report.uncaught_errors == [
        "TypeError: Cannot spawn the boss: its level data is missing\n"
        "    at spawnBoss (main.js:39:9)"
    ]
    assert report.console_errors == []


async def test_blocked_requests_are_reported(browser: HarnessBrowser) -> None:
    report = await smoke(browser, FIXTURES / "cdn", QUICK)

    assert f"{BLOCKED_REQUEST}https://cdn.example.com/style.css" in report.console_errors
    assert details(report)[NO_ERRORS].endswith(", including 1 blocked request.")
    assert report.uncaught_errors == []


async def test_a_game_without_index_html_never_becomes_ready(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    (tmp_path / "main.js").write_text("window.iterplay = { ready: true };\n")

    report = await smoke(browser, tmp_path, QUICK)

    assert {READY, STATE_SCHEMA, PLAYER_MOVES} <= failed(report)
    assert details(report)[READY] == "index.html could not be loaded: HTTP 404 Not Found"


async def test_the_ready_timeout_includes_loading_the_page(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><script>for (;;) {}</script>\n")
    started = time.monotonic()

    report = await smoke(browser, tmp_path, QUICK)

    assert failed(report) == {READY, STATE_SCHEMA, PLAYER_MOVES}
    assert details(report)[READY] == "index.html did not finish loading within 2 s"
    assert time.monotonic() - started < 10


async def test_a_game_that_becomes_ready_too_late_fails(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    game = minimal_game(tmp_path, "readyAt = 5;\nfunction step() {}\n")

    report = await smoke(browser, game, SmokeConfig(ready_timeout_s=1.0, move_hold_s=0.5))

    assert details(report)[READY] == (
        "window.iterplay.ready did not become true within 1 s: "
        "window.iterplay exists, but its ready flag is not true"
    )


async def test_a_game_that_hangs_fails_without_hanging_the_smoke_test(
    browser: HarnessBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_module, "CALL_TIMEOUT_S", 0.5)
    hang = "function step() {}\nwindow.iterplay.getState = () => { for (;;) {} };\n"
    game = minimal_game(tmp_path, hang)
    started = time.monotonic()

    report = await smoke(browser, game, QUICK)

    assert failed(report) == {STATE_SCHEMA, PLAYER_MOVES}
    assert details(report)[STATE_SCHEMA].startswith("getState() did not return within 0.5 s")
    assert time.monotonic() - started < 15


# The movement check


async def test_a_player_who_dies_and_respawns_at_the_start_still_moves(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    step = """
function step() {
  if (!held.has("ArrowRight") || deaths > 0) return;
  x += 0.05;
  if (x > 1) {
    x = 0;
    deaths = 1;
  }
}
"""
    report = await smoke(browser, minimal_game(tmp_path, step), QUICK)

    assert report.passed, report.checks
    assert details(report)[PLAYER_MOVES].startswith("The player moved from (0.00, 0.50) to (")


async def test_errors_thrown_while_the_player_moves_fail_the_error_check(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    step = """
let thrown = false;
function step() {
  if (held.has("ArrowRight")) x += 0.05;
  if (x > 0.5 && !thrown) {
    thrown = true;
    setTimeout(() => { throw new Error("the player walked into an unfinished wall"); });
  }
}
"""
    report = await smoke(browser, minimal_game(tmp_path, step), QUICK)

    assert failed(report) == {NO_ERRORS}
    assert report.uncaught_errors[0].startswith("Error: the player walked into an unfinished wall")


@pytest.mark.parametrize(
    ("controls", "moves"),
    [
        (
            ControlScheme(
                bindings={Control.LEFT: ("KeyA", "ArrowLeft"), Control.RIGHT: ("ArrowRight",)},
                directions={Control.LEFT: (-1.0, 0.0, 0.0), Control.RIGHT: (1.0, 0.0, 0.0)},
                smoke_move=Control.LEFT,
            ),
            True,
        ),
        (CONTROLS_2D, False),
    ],
    ids=["holds-smoke-move", "default-right"],
)
async def test_the_movement_check_holds_the_templates_smoke_move(
    browser: HarnessBrowser, tmp_path: Path, controls: ControlScheme, moves: bool
) -> None:
    step = 'function step() { if (held.has("KeyA")) x -= 0.05; }\n'

    report = await smoke(browser, minimal_game(tmp_path, step), QUICK, controls=controls)

    assert (PLAYER_MOVES not in failed(report)) is moves
    expected = "left (KeyA)" if moves else "right (ArrowRight)"
    assert f"while {expected} was held" in details(report)[PLAYER_MOVES]


# Configuration


@pytest.mark.parametrize(
    "config",
    [
        {"ready_timeout_s": 0.0},
        {"ready_timeout_s": -1.0},
        {"move_hold_s": 0.0},
        {"move_hold_s": float("nan")},
    ],
)
def test_the_config_needs_positive_durations(config: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SmokeConfig(**config)  # type: ignore[arg-type]


def test_the_smoke_move_must_have_a_key() -> None:
    unbound = ControlScheme(
        bindings={Control.RIGHT: ("ArrowRight",)},
        directions={Control.LEFT: (-1.0, 0.0, 0.0), Control.RIGHT: (1.0, 0.0, 0.0)},
        smoke_move=Control.LEFT,
    )

    with pytest.raises(ValueError, match="no key binding for left"):
        SmokeTester(HarnessBrowser(BrowserOptions()), unbound)
