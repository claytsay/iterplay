"""Chromium sessions (HLD §4 [2], [3]): isolation, the harness API, key presses, error capture
and recording. Every test drives real headless Chromium against pages on a loopback server."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from iterplay.contract.controls import CONTROLS_2D, Control, ControlScheme
from iterplay.contract.url import GameUrlParams
from iterplay.errors import ContractError
from iterplay.harness import browser as browser_module
from iterplay.harness.browser import (
    BLOCKED_REQUEST,
    MAX_ERRORS,
    BrowserOptions,
    GameSession,
    HarnessBrowser,
    game_url,
)
from iterplay.harness.server import game_server

pytestmark = pytest.mark.browser

FIXTURES = Path(__file__).parent / "fixtures" / "games"
VIEWPORT = BrowserOptions(viewport_width=640, viewport_height=360)
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"

KEY_LOG = """
window.keys = [];
for (const type of ["keydown", "keyup"]) {
  window.addEventListener(type, (event) => window.keys.push(`${type} ${event.code}`));
}
"""
"""Records every key event the page receives as ``"keydown ArrowRight"``."""


@pytest.fixture
async def browser() -> AsyncIterator[HarnessBrowser]:
    async with HarnessBrowser(VIEWPORT) as harness:
        yield harness


@contextmanager
def served(directory: Path) -> Iterator[str]:
    """Serve ``directory`` on 127.0.0.1 and yield its base URL."""
    with game_server(directory, port=0) as server:
        yield server.base_url


def page(directory: Path, script: str) -> Path:
    """A game directory whose ``index.html`` runs ``script`` as ``main.js``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(
        '<!doctype html><html><head><link rel="icon" href="data:,"></head>'
        '<body><script type="module" src="./main.js"></script></body></html>\n'
    )
    (directory / "main.js").write_text(script)
    return directory


@asynccontextmanager
async def opened(
    browser: HarnessBrowser,
    directory: Path,
    *,
    controls: ControlScheme = CONTROLS_2D,
    record_video_to: Path | None = None,
) -> AsyncIterator[GameSession]:
    """A session that has loaded ``directory``'s ``index.html``."""
    with served(directory) as base_url:
        async with browser.session(controls, record_video_to=record_video_to) as session:
            await session.open(f"{base_url}index.html")
            yield session


def names(directory: Path) -> list[str]:
    """The names of everything in ``directory``, hidden entries included."""
    return sorted(path.name for path in directory.iterdir())


async def evaluate(session: GameSession, expression: str) -> object:
    """Evaluate ``expression`` in the session's page, for test assertions."""
    return await session._page.evaluate(expression)


async def wait_for(session: GameSession, expression: str) -> None:
    await session._page.wait_for_function(expression, timeout=10_000)


async def crash(session: GameSession) -> None:
    """Crash the session's renderer, as an out-of-memory game would."""
    cdp = await session._page.context.new_cdp_session(session._page)
    with suppress(TimeoutError, PlaywrightError):
        await asyncio.wait_for(cdp.send("Page.crash"), 2)
    for _ in range(50):
        if session.uncaught_errors:
            return
        await asyncio.sleep(0.1)


# The harness API


async def test_get_state_returns_a_validated_snapshot(browser: HarnessBrowser) -> None:
    async with opened(browser, FIXTURES / "simple") as session:
        assert await session.wait_until_ready(10)
        state = await session.get_state()

    assert (state.player.x, state.player.y, state.player.z) == (0, 0.5, 0)
    assert state.player.grounded
    assert (state.goal.x, state.goal.y) == (12, 0.5)
    assert [(item.id, item.collected) for item in state.collectibles] == [
        ("g1", False),
        ("g2", False),
    ]
    assert (state.deaths, state.level_complete) == (0, False)


async def test_get_state_drains_the_events_it_returns(browser: HarnessBrowser) -> None:
    async with opened(browser, FIXTURES / "simple") as session:
        assert await session.wait_until_ready(10)
        await session.get_state()
        await session.hold(frozenset({Control.RESPAWN}))
        await asyncio.sleep(0.2)
        await session.release_all()
        first = await session.get_state()
        second = await session.get_state()

    assert [event.type for event in first.events] == ["respawn"]
    assert second.events == []


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("", "window.iterplay is not defined"),
        ("window.iterplay = { ready: true };", "window.iterplay.getState is not a function"),
        (
            "window.iterplay = { ready: true, getState: () => null };",
            "getState() returned null or undefined instead of a snapshot",
        ),
        (
            "window.iterplay = { ready: true, getState: () => undefined };",
            "getState() returned null or undefined instead of a snapshot",
        ),
        (
            "window.iterplay = { ready: true, getState: () => 42 };",
            "getState() returned an invalid snapshot: Input should be a valid dictionary",
        ),
        (
            "window.iterplay = { ready: true, getState: async () => ({ t: 0 }) };",
            "getState() returned a Promise; it must return the snapshot itself",
        ),
    ],
    ids=["missing", "not-a-function", "null", "undefined", "number", "async"],
)
async def test_get_state_reports_a_broken_api(
    browser: HarnessBrowser, tmp_path: Path, script: str, message: str
) -> None:
    async with opened(browser, page(tmp_path, script)) as session:
        with pytest.raises(ContractError) as caught:
            await session.get_state()

    assert str(caught.value).startswith(message)


async def test_get_state_reports_where_get_state_threw(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    script = (
        "const level = {};\nwindow.iterplay = { ready: true, getState: () => level.player.x };\n"
    )
    async with opened(browser, page(tmp_path, script)) as session:
        with pytest.raises(ContractError) as caught:
            await session.get_state()

    message = str(caught.value)
    assert message.startswith("getState() threw TypeError: Cannot read properties of undefined")
    assert "main.js:2:" in message
    assert "127.0.0.1" not in message
    assert "UtilityScript" not in message
    assert "eval at" not in message


async def test_get_state_summarises_an_invalid_snapshot(browser: HarnessBrowser) -> None:
    async with opened(browser, FIXTURES / "bad_state") as session:
        with pytest.raises(ContractError) as caught:
            await session.get_state()

    assert str(caught.value) == (
        "getState() returned an invalid snapshot: "
        "t: Input should be a valid number, unable to parse string as a number; "
        "player.y: Field required; player.vx: Field required (and 6 more)"
    )


async def test_get_state_names_nested_fields_by_their_wire_names(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    script = """
window.iterplay = {
  ready: true,
  getState: () => ({
    t: 1, player: { x: 0, y: 0, vx: 0, vy: 0, grounded: true }, goal: { x: 1, y: 0 },
    collectibles: [{ id: "a", x: 1, y: 1, collected: false }, { id: "b", x: 1, y: 1 }],
    deaths: 0, levelComplete: "nope", fps: 60, events: [],
  }),
};
"""
    async with opened(browser, page(tmp_path, script)) as session:
        with pytest.raises(ContractError) as caught:
            await session.get_state()

    assert str(caught.value) == (
        "getState() returned an invalid snapshot: collectibles[1].collected: Field required; "
        "levelComplete: Input should be a valid boolean, unable to interpret input"
    )


async def test_a_page_stuck_in_a_loop_times_out(
    browser: HarnessBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_module, "CALL_TIMEOUT_S", 0.5)
    script = "window.iterplay = { ready: true, getState() { for (;;) {} } };"
    started = time.monotonic()
    async with opened(browser, page(tmp_path, script)) as session:
        assert await session.wait_until_ready(5)
        with pytest.raises(ContractError, match=r"^getState\(\) did not return within 0.5 s"):
            await session.get_state()
        with pytest.raises(ContractError, match=r"^Pressing ArrowRight did not return"):
            await session.hold(frozenset({Control.RIGHT}))
        assert not await session.wait_until_ready(0.2)

    assert time.monotonic() - started < 10


# Readiness


async def test_wait_until_ready_waits_for_the_flag(browser: HarnessBrowser, tmp_path: Path) -> None:
    script = (
        "window.iterplay = { ready: false };\n"
        "setTimeout(() => { window.iterplay.ready = true; }, 400);\n"
    )
    async with opened(browser, page(tmp_path, script)) as session:
        started = time.monotonic()
        assert await session.wait_until_ready(5)
        waited = time.monotonic() - started

    assert 0.3 <= waited < 3


@pytest.mark.parametrize(
    "script",
    [
        "",
        "window.iterplay = { ready: 1 };",
        "window.iterplay = { ready: 'true' };",
        "Object.defineProperty(window, 'iterplay', { get() { throw new Error('no'); } });",
    ],
    ids=["missing", "truthy-number", "string", "throwing-getter"],
)
async def test_wait_until_ready_gives_up_after_the_timeout(
    browser: HarnessBrowser, tmp_path: Path, script: str
) -> None:
    async with opened(browser, page(tmp_path, script)) as session:
        started = time.monotonic()
        assert not await session.wait_until_ready(0.5)
        waited = time.monotonic() - started

    assert 0.45 <= waited < 2.5


# Key presses


async def test_hold_presses_and_releases_only_what_changed(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    async with opened(browser, page(tmp_path, KEY_LOG)) as session:
        await session.hold(frozenset({Control.RIGHT}))
        await session.hold(frozenset({Control.RIGHT}))
        await session.hold(frozenset({Control.RIGHT, Control.JUMP}))
        await session.hold(frozenset({Control.JUMP, Control.LEFT}))
        await session.hold(frozenset({Control.RESPAWN}))
        await session.release_all()
        await session.release_all()
        keys = await evaluate(session, "window.keys")

    assert keys == [
        "keydown ArrowRight",
        "keydown Space",
        "keyup ArrowRight",
        "keydown ArrowLeft",
        "keyup ArrowLeft",
        "keyup Space",
        "keydown KeyR",
        "keyup KeyR",
    ]


async def test_hold_uses_the_session_control_scheme(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    scheme = ControlScheme(
        bindings={Control.FORWARD: ("KeyW", "ArrowUp"), Control.JUMP: ("KeyJ",)},
        directions={Control.FORWARD: (0.0, 0.0, -1.0)},
        smoke_move=Control.FORWARD,
    )
    async with opened(browser, page(tmp_path, KEY_LOG), controls=scheme) as session:
        await session.hold(frozenset({Control.FORWARD, Control.JUMP}))
        keys = await evaluate(session, "window.keys")

    assert keys == ["keydown KeyW", "keydown KeyJ"]


async def test_hold_rejects_an_unbound_control_before_pressing_anything(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    async with opened(browser, page(tmp_path, KEY_LOG)) as session:
        with pytest.raises(ValueError, match="no key binding for forward"):
            await session.hold(frozenset({Control.RIGHT, Control.FORWARD}))
        keys = await evaluate(session, "window.keys")

    assert keys == []


async def test_held_keys_move_the_player(browser: HarnessBrowser) -> None:
    async with opened(browser, FIXTURES / "simple") as session:
        assert await session.wait_until_ready(10)
        await session.hold(frozenset({Control.RIGHT}))
        await asyncio.sleep(0.5)
        moving = await session.get_state()
        await session.release_all()
        await asyncio.sleep(0.3)
        stopped = await session.get_state()
        await asyncio.sleep(0.2)
        still = await session.get_state()

    assert moving.player.x > 1
    assert moving.player.vx > 0
    assert stopped.player.vx == 0
    assert still.player.x == stopped.player.x


# Console and errors


async def test_console_messages_are_recorded(browser: HarnessBrowser, tmp_path: Path) -> None:
    script = (
        'console.log("hello", 42);\n'
        'console.warn("careful");\n'
        'console.error("it broke");\n'
        'console.assert(1 === 2, "one is not two");\n'
        "window.logged = true;\n"
    )
    async with opened(browser, page(tmp_path, script)) as session:
        await wait_for(session, "window.logged === true")
        lines, errors = session.console_lines, session.console_errors

    assert lines == (
        "[log] hello 42",
        "[warning] careful",
        "[error] it broke (main.js:3)",
        "[assert] Assertion failed: one is not two (main.js:4)",
    )
    assert errors == ("it broke (main.js:3)", "Assertion failed: one is not two (main.js:4)")


async def test_a_missing_resource_is_a_console_error_naming_it(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    script = 'import("./level.js").catch(() => { window.failed = true; });\n'
    async with opened(browser, page(tmp_path, script)) as session:
        await wait_for(session, "window.failed === true")
        errors = session.console_errors

    assert errors == (
        "Failed to load resource: the server responded with a status of 404 (Not Found) (level.js)",
    )


async def test_uncaught_errors_keep_their_first_frames(browser: HarnessBrowser) -> None:
    async with opened(browser, FIXTURES / "throws") as session:
        await wait_for(session, "window.iterplay.ready === true")
        await asyncio.sleep(0.6)
        uncaught, lines = session.uncaught_errors, session.console_lines

    assert uncaught == (
        "TypeError: Cannot spawn the boss: its level data is missing\n"
        "    at spawnBoss (main.js:39:9)",
    )
    assert lines == (f"[uncaught] {uncaught[0]}",)
    assert session.console_errors == ()


async def test_uncaught_errors_of_every_kind_are_recorded(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    script = """
function dig(depth) {
  if (depth === 0) throw new RangeError("too deep");
  dig(depth - 1);
}
setTimeout(() => dig(8));
setTimeout(() => { throw "a plain string"; });
setTimeout(() => Promise.reject(new Error("nobody caught this")));
setTimeout(() => { window.thrown = true; }, 50);
"""
    async with opened(browser, page(tmp_path, script)) as session:
        await wait_for(session, "window.thrown === true")
        await asyncio.sleep(0.2)
        deep, plain, rejected = session.uncaught_errors

    assert deep.splitlines() == [
        "RangeError: too deep",
        "    at dig (main.js:3:26)",
        "    at dig (main.js:4:3)",
        "    at dig (main.js:4:3)",
    ]
    assert plain == "a plain string"
    assert rejected.splitlines()[0] == "Error: nobody caught this"


async def test_error_lists_and_console_lines_are_capped(
    browser: HarnessBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_module, "MAX_CONSOLE_LINES", 150)
    script = f"""
for (let i = 0; i < {MAX_ERRORS + 20}; i++) {{
  console.error(`console ${{i}}`);
  setTimeout(() => {{ throw new Error(`uncaught ${{i}}`); }});
}}
setTimeout(() => {{ window.done = true; }}, 100);
"""
    async with opened(browser, page(tmp_path, script)) as session:
        await wait_for(session, "window.done === true")
        await asyncio.sleep(0.3)
        uncaught, errors, lines = (
            session.uncaught_errors,
            session.console_errors,
            session.console_lines,
        )

    assert len(uncaught) == len(errors) == MAX_ERRORS
    assert uncaught[0].startswith("Error: uncaught 0")
    assert errors[-1] == f"console {MAX_ERRORS - 1} (main.js:3)"
    assert len(lines) == 151
    assert lines[-1] == "[harness] 90 more console messages were not recorded"


async def test_a_crashed_page_is_recorded_and_fails_every_call(
    browser: HarnessBrowser,
) -> None:
    async with opened(browser, FIXTURES / "simple") as session:
        assert await session.wait_until_ready(10)
        await crash(session)
        with pytest.raises(ContractError, match=r"^getState\(\) failed: the page crashed$"):
            await session.get_state()
        started = time.monotonic()
        assert not await session.wait_until_ready(5)
        assert time.monotonic() - started < 2
        uncaught, lines = session.uncaught_errors, session.console_lines

    assert uncaught == ("The page crashed.",)
    assert lines == ("[crash] The page crashed.",)


# Network isolation


async def test_only_the_games_own_origin_is_reachable(
    browser: HarnessBrowser, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret.json").write_text("{}")
    game = page(tmp_path / "game", "")
    (game / "level.json").write_text('{"ok": true}')
    with served(other) as other_url:
        (game / "main.js").write_text(
            f"""
const results = {{}};
async function attempt(name, url) {{
  try {{
    await fetch(url);
    results[name] = "loaded";
  }} catch {{
    results[name] = "blocked";
  }}
}}
await attempt("same origin", "./level.json");
await attempt("cdn", "https://cdn.example.com/three.module.js");
await attempt("other port", "{other_url}secret.json");
navigator.sendBeacon("https://telemetry.example.com/beacon", "data");
const socket = new WebSocket("wss://live.example.com/socket");
socket.onclose = () => {{
  results.socket = "closed";
  window.results = results;
}};
"""
        )
        async with opened(browser, game) as session:
            await wait_for(session, "window.results !== undefined")
            results = await evaluate(session, "window.results")
            for _ in range(50):
                blocked = [e for e in session.console_errors if e.startswith(BLOCKED_REQUEST)]
                if len(blocked) == 4:
                    break
                await asyncio.sleep(0.1)
            lines = session.console_lines

    assert results == {
        "same origin": "loaded",
        "cdn": "blocked",
        "other port": "blocked",
        "socket": "closed",
    }
    assert sorted(blocked) == sorted(
        f"{BLOCKED_REQUEST}{url}"
        for url in (
            "https://cdn.example.com/three.module.js",
            f"{other_url}secret.json",
            "https://telemetry.example.com/beacon",
            "wss://live.example.com/socket",
        )
    )
    assert f"[error] {BLOCKED_REQUEST}https://cdn.example.com/three.module.js" in lines
    assert not any("secret.json" in record.getMessage() for record in caplog.records)
    assert any("level.json" in record.getMessage() for record in caplog.records)


async def test_inline_urls_are_allowed(browser: HarnessBrowser, tmp_path: Path) -> None:
    script = """
const blob = new Blob(["export const value = 2;"], { type: "text/javascript" });
const fromBlob = await import(URL.createObjectURL(blob));
const fromData = await import("data:text/javascript,export const value = 3;");
const text = await (await fetch("data:text/plain,hello")).text();
window.result = [fromBlob.value, fromData.value, text];
"""
    async with opened(browser, page(tmp_path, script)) as session:
        await wait_for(session, "window.result !== undefined")
        result = await evaluate(session, "window.result")
        errors = session.console_errors

    assert result == [2, 3, "hello"]
    assert errors == ()


async def test_sessions_do_not_share_storage(browser: HarnessBrowser, tmp_path: Path) -> None:
    game = page(
        tmp_path, "window.seen = localStorage.getItem('best'); localStorage.setItem('best', '9');"
    )
    with served(game) as base_url:
        seen = []
        for _ in range(2):
            async with browser.session(CONTROLS_2D) as session:
                await session.open(f"{base_url}index.html")
                await wait_for(session, "window.seen !== undefined")
                seen.append(await evaluate(session, "window.seen"))

    assert seen == [None, None]


# Navigation


@pytest.mark.parametrize(
    "url", ["file:///etc/hosts", "about:blank", "data:text/html,hi", "http://", "ws://127.0.0.1/"]
)
async def test_open_accepts_only_http_urls(browser: HarnessBrowser, url: str) -> None:
    async with browser.session(CONTROLS_2D) as session:
        with pytest.raises(ValueError, match="http or https"):
            await session.open(url)


async def test_open_needs_a_positive_timeout(browser: HarnessBrowser) -> None:
    async with browser.session(CONTROLS_2D) as session:
        with pytest.raises(ValueError, match="timeout_s must be positive"):
            await session.open("http://127.0.0.1:1/index.html", timeout_s=0)


async def test_open_reports_an_http_error(browser: HarnessBrowser, tmp_path: Path) -> None:
    with served(page(tmp_path, "")) as base_url:
        async with browser.session(CONTROLS_2D) as session:
            with pytest.raises(ContractError) as caught:
                await session.open(f"{base_url}missing.html?seed=1")

    assert str(caught.value) == "missing.html could not be loaded: HTTP 404 Not Found"


async def test_open_reports_a_page_that_never_finishes_loading(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><script>for (;;) {}</script>\n")
    started = time.monotonic()
    with served(tmp_path) as base_url:
        async with browser.session(CONTROLS_2D) as session:
            with pytest.raises(ContractError) as caught:
                await session.open(f"{base_url}index.html", timeout_s=1)

    assert str(caught.value) == "index.html did not finish loading within 1 s"
    assert time.monotonic() - started < 10


async def test_open_reports_a_server_that_is_gone(browser: HarnessBrowser, tmp_path: Path) -> None:
    with served(page(tmp_path, "")) as base_url:
        pass
    async with browser.session(CONTROLS_2D) as session:
        with pytest.raises(ContractError, match=r"^index\.html could not be loaded: net::ERR_"):
            await session.open(f"{base_url}index.html")


# Recording and lifecycle


async def test_video_t_starts_with_the_page(browser: HarnessBrowser) -> None:
    async with browser.session(CONTROLS_2D) as session:
        start = session.video_t()
        await asyncio.sleep(0.3)
        later = session.video_t()

    assert 0 <= start < 1
    assert 0.25 <= later - start < 1


async def test_viewport_matches_the_options(browser: HarnessBrowser, tmp_path: Path) -> None:
    async with opened(browser, page(tmp_path, "")) as session:
        size = await evaluate(session, "[window.innerWidth, window.innerHeight]")

    assert size == [VIEWPORT.viewport_width, VIEWPORT.viewport_height]


async def test_a_recorded_session_moves_its_video_into_place(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    target = tmp_path / "runs" / "v1" / "playtest" / "random.webm"
    async with opened(browser, FIXTURES / "simple", record_video_to=target) as session:
        assert await session.wait_until_ready(10)
        await asyncio.sleep(0.5)
        assert not target.exists()

    assert target.read_bytes().startswith(WEBM_MAGIC)
    assert target.stat().st_size > 1000
    assert names(target.parent) == ["random.webm"]


async def test_a_recording_replaces_an_older_file(browser: HarnessBrowser, tmp_path: Path) -> None:
    target = tmp_path / "speedrunner.webm"
    target.write_bytes(b"an older recording")
    async with opened(browser, FIXTURES / "simple", record_video_to=target) as session:
        assert await session.wait_until_ready(10)

    assert target.read_bytes().startswith(WEBM_MAGIC)
    assert target.stat().st_size > 1000
    assert names(tmp_path) == ["speedrunner.webm"]


async def test_a_failed_session_keeps_no_video(browser: HarnessBrowser, tmp_path: Path) -> None:
    target = tmp_path / "playtest" / "newbie.webm"
    with pytest.raises(RuntimeError, match="bot exploded"):
        async with opened(browser, FIXTURES / "simple", record_video_to=target):
            raise RuntimeError("bot exploded")

    assert names(target.parent) == []


async def test_an_unrecorded_session_writes_nothing(
    browser: HarnessBrowser, tmp_path: Path
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "index.html").write_text("<!doctype html><title>quiet</title>\n")
    async with opened(browser, game) as session:
        assert session._page.video is None

    assert names(tmp_path) == ["game"]


async def test_closing_the_browser_closes_open_sessions(tmp_path: Path) -> None:
    harness = HarnessBrowser(VIEWPORT)
    await harness.__aenter__()
    with served(FIXTURES / "simple") as base_url:
        session_context = harness.session(CONTROLS_2D, record_video_to=tmp_path / "late.webm")
        session = await session_context.__aenter__()
        await session.open(f"{base_url}index.html")
        await harness.__aexit__(None, None, None)
        with pytest.raises(ContractError):
            await session.get_state()
        await session_context.__aexit__(None, None, None)


async def test_a_running_browser_cannot_be_entered_again(browser: HarnessBrowser) -> None:
    with pytest.raises(RuntimeError, match="already running"):
        await browser.__aenter__()


async def test_a_browser_can_run_again_after_closing() -> None:
    harness = HarnessBrowser(VIEWPORT)
    for _ in range(2):
        async with harness, harness.session(CONTROLS_2D) as session:
            assert session.video_t() >= 0


async def test_sessions_need_a_running_browser() -> None:
    with pytest.raises(RuntimeError, match="not running"):
        async with HarnessBrowser(VIEWPORT).session(CONTROLS_2D):
            pass


# URLs


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8737/", "http://127.0.0.1:8737/index.html?seed=7&fixedStep=1"),
        ("http://127.0.0.1:8737", "http://127.0.0.1:8737/index.html?seed=7&fixedStep=1"),
        (
            "http://127.0.0.1:8737/v2/game",
            "http://127.0.0.1:8737/v2/game/index.html?seed=7&fixedStep=1",
        ),
    ],
)
def test_game_url_appends_index_html_and_the_parameters(base_url: str, expected: str) -> None:
    params = GameUrlParams(seed=7, fixed_step=True, harness_hud=False)

    assert game_url(base_url, params) == expected


def test_game_url_includes_the_hud_and_bot() -> None:
    params = GameUrlParams(seed=3, bot="newbie")

    assert game_url("http://127.0.0.1:1/", params) == (
        "http://127.0.0.1:1/index.html?seed=3&fixedStep=1&harness=1&bot=newbie"
    )
