"""Chromium sessions shared by the smoke test and the playtest runner (HLD §4 [2], [3]).

Each session is its own browser context. A context only lets the page load from the origin of
the URL it opened (the local game server); any other request is blocked and recorded as a
console error, so a game that reaches for a CDN fails its smoke test.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self, TypeVar
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.layout import ENTRY_HTML
from iterplay.contract.state import HARNESS_GLOBAL, GameState
from iterplay.contract.url import GameUrlParams
from iterplay.errors import ContractError

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        ConsoleMessage,
        Page,
        Playwright,
        Route,
        SourceLocation,
        ViewportSize,
        WebSocketRoute,
    )

_T = TypeVar("_T")

_log = logging.getLogger(__name__)

_LAUNCH_ARGS: Final = ("--enable-unsafe-swiftshader",)
"""Lets Chromium fall back to software WebGL (SwiftShader) where it has no GPU, as when
headless, so generated games can render."""

LOAD_TIMEOUT_S: Final = 30.0
"""Default limit on :meth:`GameSession.open` waiting for the page's load event."""

CALL_TIMEOUT_S: Final = 5.0
"""Longest one call into the page (``getState()``, a key press) may take. A page that does not
answer in time is stuck, for example in an endless loop."""

_POLL_INTERVAL_S: Final = 0.1
_MIN_POLL_TIMEOUT_S: Final = 0.5

MAX_ERRORS: Final = 100
"""Uncaught errors, and console errors, kept per session. A game that throws every frame
would otherwise collect thousands of copies of one message."""

MAX_CONSOLE_LINES: Final = 5000
"""Console lines kept per session; the rest are only counted."""

_STACK_FRAMES: Final = 3
"""Stack frames kept in each error: enough to locate the fault in the game's code."""

_SUMMARY_PROBLEMS: Final = 3
"""Validation problems quoted when a snapshot is invalid; the rest are only counted."""

_ERROR_TYPES: Final = frozenset({"error", "assert"})
"""Console message types that count as errors: ``console.error`` and failed assertions."""

_INLINE_SCHEMES: Final = frozenset({"data", "blob"})
"""URL schemes that never reach the network, so they are always allowed."""

_DEFAULT_PORTS: Final[Mapping[str, int]] = {"http": 80, "https": 443}

BLOCKED_REQUEST: Final = "blocked request: "
"""Prefix of the console error recorded for each blocked request, followed by its URL."""

_CRASHED: Final = "The page crashed."

_FRAME: Final = re.compile(r"\s+at\s")
"""A frame line of a V8 stack trace."""

_API_PREFIX: Final = re.compile(r"^\w+\.\w+: ")
"""The ``Page.evaluate: `` style prefix on Playwright error messages."""

_READY_SCRIPT: Final = """name => {
  try {
    const api = window[name];
    return api !== undefined && api !== null && api.ready === true;
  } catch {
    return false;
  }
}"""

_STATE_SCRIPT: Final = """name => {
  const describe = (error) => {
    try {
      return error instanceof Error && error.stack ? error.stack : String(error);
    } catch {
      return "an unprintable value";
    }
  };
  try {
    const api = window[name];
    if (api === undefined || api === null) {
      return { problem: `window.${name} is not defined` };
    }
    if (typeof api.getState !== "function") {
      return { problem: `window.${name}.getState is not a function` };
    }
    const state = api.getState();
    if (state !== null && typeof state === "object" && typeof state.then === "function") {
      return { problem: "getState() returned a Promise; it must return the snapshot itself" };
    }
    return { state };
  } catch (error) {
    return { problem: `getState() threw ${describe(error)}` };
  }
}"""
"""Calls ``getState()``, reporting a missing API or a thrown error instead of failing."""


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    headless: bool = True
    """Headless Chromium falls back to software WebGL; run headed to measure performance."""

    viewport_width: int = 1280
    viewport_height: int = 720


def game_url(base_url: str, params: GameUrlParams) -> str:
    """The URL of the game's ``index.html`` under ``base_url``, with ``params`` as its query.

    For example ``http://127.0.0.1:8737/index.html?seed=1&fixedStep=1``.
    """
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return f"{urljoin(base, ENTRY_HTML)}?{params.to_query()}"


class GameSession:
    """One game page in an isolated, optionally recorded, browser context.

    Abstract controls become real key presses through the game template's control scheme.
    Sessions are created by :meth:`HarnessBrowser.session` as soon as their page exists,
    which is when recording starts, so :meth:`video_t` follows the video's own clock.

    Errors are recorded as text: the message and the first few stack frames, with the game's
    own URLs shortened to file names (``main.js:12:5``). Calls into the page that fail,
    because the game broke the contract, crashed or hangs, raise
    :class:`~iterplay.errors.ContractError`.
    """

    def __init__(self, page: Page, controls: ControlScheme) -> None:
        self._page = page
        self._controls = controls
        self._started = time.monotonic()
        self._origin: _Origin | None = None
        self._isolated = False
        self._keys_down: dict[Control, str] = {}
        self._uncaught: list[str] = []
        self._errors: list[str] = []
        self._lines: list[str] = []
        self._dropped_lines = 0
        self._crashed = False
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("crash", self._on_crash)

    @property
    def uncaught_errors(self) -> tuple[str, ...]:
        """Uncaught exceptions thrown by the page so far, and its crash if it crashed.

        Only the first :data:`MAX_ERRORS` are kept.
        """
        return tuple(self._uncaught)

    @property
    def console_errors(self) -> tuple[str, ...]:
        """``console.error`` messages and blocked requests so far.

        Messages the browser logs about a resource name it, e.g. ``... 404 (Not Found)
        (level.js)``. Only the first :data:`MAX_ERRORS` are kept.
        """
        return tuple(self._errors)

    @property
    def console_lines(self) -> tuple[str, ...]:
        """Every console message so far, for ``console.log``.

        Each line starts with its type, e.g. ``[log] ...`` or ``[error] ...``. Uncaught errors
        appear as ``[uncaught] ...`` and a crash as ``[crash] ...``. After
        :data:`MAX_CONSOLE_LINES`, a final ``[harness]`` line counts the messages left out.
        """
        if not self._dropped_lines:
            return tuple(self._lines)
        dropped = f"[harness] {self._dropped_lines} more console messages were not recorded"
        return (*self._lines, dropped)

    async def open(self, url: str, *, timeout_s: float = LOAD_TIMEOUT_S) -> None:
        """Navigate to the game and wait for its load event. Only ``url``'s origin may serve
        subsequent requests.

        Raises:
            ValueError: ``url`` is not an http or https URL, or ``timeout_s`` is not positive.
            ContractError: The page failed to load, answered with an HTTP error, or did not
                finish loading within ``timeout_s``.
        """
        origin = _Origin.of(url)
        if origin is None:
            raise ValueError(f"A game URL must be an http or https URL, got {url!r}")
        if not timeout_s > 0:
            raise ValueError(f"timeout_s must be positive, got {timeout_s}")
        self._origin = origin
        await self._isolate()
        name = urlsplit(url).path.lstrip("/") or "/"
        try:
            response = await self._page.goto(url, wait_until="load", timeout=timeout_s * 1000)
        except PlaywrightTimeoutError:
            raise ContractError(f"{name} did not finish loading within {timeout_s:g} s") from None
        except PlaywrightError as exc:
            raise ContractError(f"{name} could not be loaded: {self._reason(exc)}") from exc
        if response is not None and response.status >= 400:
            status = f"HTTP {response.status} {response.status_text}".rstrip()
            raise ContractError(f"{name} could not be loaded: {status}")

    async def wait_until_ready(self, timeout_s: float) -> bool:
        """Poll ``window.iterplay.ready`` about ten times a second; False if it is still not
        true after ``timeout_s``, or as soon as the page crashes."""
        deadline = time.monotonic() + timeout_s
        while not await self._is_ready(deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._crashed:
                return False
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
        return True

    async def get_state(self) -> GameState:
        """Call ``window.iterplay.getState()`` once. This drains the game's pending events.

        Raises:
            ContractError: ``window.iterplay`` is missing, or the snapshot fails validation.
                Also when ``getState()`` throws, returns nothing, or does not return within
                :data:`CALL_TIMEOUT_S`, and when the page has crashed or closed.
        """
        reply = await self._call("getState()", self._page.evaluate(_STATE_SCRIPT, HARNESS_GLOBAL))
        if not isinstance(reply, dict):
            raise ContractError("getState() could not be called")
        problem = reply.get("problem")
        if problem is not None:
            raise ContractError(self._shorten(_brief_stack(str(problem))))
        payload = reply.get("state")
        if payload is None:
            raise ContractError("getState() returned null or undefined instead of a snapshot")
        try:
            return GameState.model_validate(payload)
        except ValidationError as exc:
            summary = _validation_summary(exc)
            raise ContractError(f"getState() returned an invalid snapshot: {summary}") from None

    async def hold(self, held: frozenset[Control]) -> None:
        """Hold exactly ``held``: press newly held keys and release all others.

        Each control is held through the first key bound to it.

        Raises:
            ValueError: A control has no binding in this session's control scheme. No key is
                pressed or released.
            ContractError: The page did not take the key events, because it crashed, closed
                or hangs.
        """
        keys = {control: self._key(control) for control in held}
        for control in [c for c in Control if c in self._keys_down and c not in held]:
            key = self._keys_down[control]
            await self._call(f"Releasing {key}", self._page.keyboard.up(key))
            del self._keys_down[control]
        for control in [c for c in Control if c in keys and c not in self._keys_down]:
            key = keys[control]
            await self._call(f"Pressing {key}", self._page.keyboard.down(key))
            self._keys_down[control] = key

    async def release_all(self) -> None:
        """Release every held control.

        Raises:
            ContractError: The page did not take the key events.
        """
        await self.hold(frozenset())

    def video_t(self) -> float:
        """Seconds since this session's recording started."""
        return time.monotonic() - self._started

    def _key(self, control: Control) -> str:
        keys = self._controls.bindings.get(control)
        if not keys:
            raise ValueError(f"The control scheme has no key binding for {control}")
        return keys[0]

    async def _isolate(self) -> None:
        """Route every request and WebSocket of this session's context, once."""
        if self._isolated:
            return
        context = self._page.context
        await context.route(_every_url, self._route)
        await context.route_web_socket(_every_url, self._refuse_web_socket)
        self._isolated = True

    async def _route(self, route: Route) -> None:
        url = route.request.url
        allowed = self._allows(url)
        if not allowed:
            self._block(url)
        with suppress(PlaywrightError):
            if allowed:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

    async def _refuse_web_socket(self, route: WebSocketRoute) -> None:
        """WebSockets are always refused: the game server only serves files."""
        self._block(route.url)
        with suppress(PlaywrightError):
            await route.close(code=1008, reason="blocked by the iterplay harness")

    def _allows(self, url: str) -> bool:
        scheme = url.partition(":")[0].lower()
        if scheme in _INLINE_SCHEMES:
            return True
        return self._origin is not None and _Origin.of(url) == self._origin

    def _block(self, url: str) -> None:
        _log.debug("Blocked a request for %s", url)
        message = f"{BLOCKED_REQUEST}{url}"
        _keep(self._errors, message, MAX_ERRORS)
        self._add_line("error", message)

    async def _is_ready(self, deadline: float) -> bool:
        timeout_s = max(deadline - time.monotonic(), _MIN_POLL_TIMEOUT_S)
        try:
            ready = await asyncio.wait_for(
                self._page.evaluate(_READY_SCRIPT, HARNESS_GLOBAL), timeout_s
            )
        except (TimeoutError, PlaywrightError):
            return False
        return ready is True

    async def _call(self, action: str, call: Awaitable[_T]) -> _T:
        """Await one call into the page, turning a crashed, closed or stuck page into a
        :class:`ContractError`."""
        try:
            return await asyncio.wait_for(call, CALL_TIMEOUT_S)
        except TimeoutError:
            raise ContractError(
                f"{action} did not return within {CALL_TIMEOUT_S:g} s; the page may be stuck "
                "in an endless loop"
            ) from None
        except PlaywrightError as exc:
            raise ContractError(f"{action} failed: {self._reason(exc)}") from exc

    def _reason(self, error: PlaywrightError) -> str:
        if self._crashed:
            return "the page crashed"
        lines = error.message.strip().splitlines()
        return self._shorten(_API_PREFIX.sub("", lines[0])) if lines else "unknown error"

    def _on_console(self, message: ConsoleMessage) -> None:
        text = self._shorten(message.text)
        if message.type == "assert":
            text = f"Assertion failed: {text}" if text else "Assertion failed"
        if message.type in _ERROR_TYPES:
            where = self._where(message.location)
            if where:
                text = f"{text} ({where})"
            _keep(self._errors, text, MAX_ERRORS)
        self._add_line(message.type, text)

    def _on_page_error(self, error: PlaywrightError) -> None:
        text = self._shorten(_error_text(error))
        _keep(self._uncaught, text, MAX_ERRORS)
        self._add_line("uncaught", text)

    def _on_crash(self, _page: Page) -> None:
        self._crashed = True
        _keep(self._uncaught, _CRASHED, MAX_ERRORS)
        self._add_line("crash", _CRASHED)

    def _add_line(self, kind: str, text: str) -> None:
        if len(self._lines) < MAX_CONSOLE_LINES:
            self._lines.append(f"[{kind}] {text}")
        else:
            self._dropped_lines += 1

    def _where(self, location: SourceLocation) -> str:
        """Where a console message came from, e.g. ``main.js:12``, or the resource it is
        about; "" when unknown."""
        url = location.get("url") or ""
        if not url:
            return ""
        line, column = location.get("lineNumber", 0), location.get("columnNumber", 0)
        where = self._shorten(url)
        return f"{where}:{line + 1}" if line or column else where

    def _shorten(self, text: str) -> str:
        """``text`` with the game's own URLs reduced to their paths: ``main.js:12:5``."""
        if self._origin is None:
            return text
        return text.replace(f"{self._origin}/", "")


class HarnessBrowser:
    """Owns one Chromium instance; every session gets its own context."""

    def __init__(self, options: BrowserOptions) -> None:
        self._options = options
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: set[BrowserContext] = set()

    async def __aenter__(self) -> Self:
        """Launch Chromium.

        Raises:
            RuntimeError: The browser is already running.
        """
        if self._playwright is not None:
            raise RuntimeError("The harness browser is already running")
        playwright = await async_playwright().start()
        try:
            self._browser = await playwright.chromium.launch(
                headless=self._options.headless, args=list(_LAUNCH_ARGS)
            )
        except BaseException:
            await playwright.stop()
            raise
        self._playwright = playwright
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close Chromium and every open context."""
        playwright, browser, contexts = self._playwright, self._browser, list(self._contexts)
        self._playwright, self._browser = None, None
        self._contexts.clear()
        try:
            for context in contexts:
                with suppress(PlaywrightError):
                    await context.close()
            if browser is not None:
                with suppress(PlaywrightError):
                    await browser.close()
        finally:
            if playwright is not None:
                await playwright.stop()

    def session(
        self, controls: ControlScheme, *, record_video_to: Path | None = None
    ) -> AbstractAsyncContextManager[GameSession]:
        """Open an isolated context with one page, driven through ``controls``.

        When recording, closing the context finalises the video and moves it to
        ``record_video_to``, creating its parent directories and replacing any older file. The
        video is kept only when the ``async with`` body exits normally.

        Raises:
            RuntimeError: On entering, if the browser is not running.
        """
        return self._session(controls, record_video_to)

    @asynccontextmanager
    async def _session(
        self, controls: ControlScheme, record_video_to: Path | None
    ) -> AsyncIterator[GameSession]:
        browser = self._running()
        viewport: ViewportSize = {
            "width": self._options.viewport_width,
            "height": self._options.viewport_height,
        }
        video_dir = None if record_video_to is None else _video_dir(record_video_to)
        try:
            context = await browser.new_context(
                viewport=viewport,
                record_video_dir=video_dir,
                record_video_size=None if video_dir is None else viewport,
                service_workers="block",
            )
            self._contexts.add(context)
            try:
                page = await context.new_page()
                yield GameSession(page, controls)
            finally:
                self._contexts.discard(context)
                with suppress(PlaywrightError):
                    await context.close()
            if record_video_to is not None:
                await _keep_video(page, record_video_to)
        finally:
            if video_dir is not None:
                shutil.rmtree(video_dir, ignore_errors=True)

    def _running(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("The harness browser is not running; use it with 'async with'")
        return self._browser


@dataclass(frozen=True, slots=True)
class _Origin:
    """The scheme, host and port of an http(s) URL."""

    scheme: str
    host: str
    port: int

    @classmethod
    def of(cls, url: str) -> Self | None:
        """The origin of ``url``; None unless it is an http(s) URL with a valid host and port."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        default = _DEFAULT_PORTS.get(scheme)
        if default is None or not parts.hostname:
            return None
        return cls(scheme, parts.hostname, default if port is None else port)

    def __str__(self) -> str:
        """The origin as the browser writes it, e.g. ``http://127.0.0.1:8737``."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == _DEFAULT_PORTS[self.scheme] else f":{self.port}"
        return f"{self.scheme}://{host}{port}"


def _every_url(url: str) -> bool:
    return True


def _keep(messages: list[str], message: str, limit: int) -> None:
    if len(messages) < limit:
        messages.append(message)


def _error_text(error: PlaywrightError) -> str:
    """An uncaught error as text: its stack when it has one, else its name and message."""
    stack = error.stack or ""
    if stack.strip():
        return _brief_stack(stack)
    name, message = error.name or "", error.message or ""
    if name and message:
        return f"{name}: {message}"
    return message or name or "(no message)"


def _brief_stack(text: str) -> str:
    """``text`` with any V8 stack trace in it cut to its first few frames in the game's code.

    Frames without a URL, such as the harness's own evaluation wrapper, are left out.
    """
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if _FRAME.match(line)), len(lines))
    frames = [line.strip() for line in lines[first:] if _FRAME.match(line) and "://" in line]
    return "\n".join([*lines[:first], *(f"    {frame}" for frame in frames[:_STACK_FRAMES])])


def _validation_summary(error: ValidationError) -> str:
    """The first few problems in an invalid snapshot, e.g. ``player.grounded: Field
    required``."""
    problems = [_problem(detail) for detail in error.errors()]
    summary = "; ".join(problems[:_SUMMARY_PROBLEMS])
    more = len(problems) - _SUMMARY_PROBLEMS
    return f"{summary} (and {more} more)" if more > 0 else summary


def _problem(detail: ErrorDetails) -> str:
    """One validation problem, located with the snapshot's own field names."""
    location = ""
    for part in detail["loc"]:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += f".{part}" if location else part
    return f"{location}: {detail['msg']}" if location else detail["msg"]


def _video_dir(target: Path) -> str:
    """A private directory beside ``target`` for Playwright to record into, so that moving
    the finished video into place is a rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".recording", dir=target.parent)


async def _keep_video(page: Page, target: Path) -> None:
    video = page.video
    if video is None:
        raise RuntimeError("The session was not recorded")
    source = await video.path()
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
