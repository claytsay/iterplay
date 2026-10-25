"""Chromium sessions shared by the smoke test and the playtest runner (HLD §4 [2], [3]).

Each session is its own browser context. A context only lets the page load from the origin of
the URL it opened (the local game server); any other request is blocked and recorded as a
console error, so a game that reaches for a CDN fails its smoke test.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from iterplay.contract.controls import Control, ControlScheme
from iterplay.contract.state import GameState

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    headless: bool = True
    """Headless Chromium falls back to software WebGL; run headed to measure performance."""

    viewport_width: int = 1280
    viewport_height: int = 720


class GameSession:
    """One game page in an isolated, optionally recorded, browser context.

    Abstract controls become real key presses through the game template's control scheme.
    """

    def __init__(self, page: Page, controls: ControlScheme) -> None:
        self._page = page
        self._controls = controls

    @property
    def uncaught_errors(self) -> tuple[str, ...]:
        """Uncaught exceptions thrown by the page so far."""
        raise NotImplementedError

    @property
    def console_errors(self) -> tuple[str, ...]:
        """``console.error`` messages and blocked requests so far."""
        raise NotImplementedError

    @property
    def console_lines(self) -> tuple[str, ...]:
        """Every console message so far, for ``console.log``."""
        raise NotImplementedError

    async def open(self, url: str) -> None:
        """Navigate to the game. Only ``url``'s origin may serve subsequent requests."""
        raise NotImplementedError

    async def wait_until_ready(self, timeout_s: float) -> bool:
        """Poll ``window.iterplay.ready``; False if it is still not true after ``timeout_s``."""
        raise NotImplementedError

    async def get_state(self) -> GameState:
        """Call ``window.iterplay.getState()`` once. This drains the game's pending events.

        Raises:
            ContractError: ``window.iterplay`` is missing, or the snapshot fails validation.
        """
        raise NotImplementedError

    async def hold(self, held: frozenset[Control]) -> None:
        """Hold exactly ``held``: press newly held keys and release all others.

        Raises:
            ValueError: A control has no binding in this session's control scheme.
        """
        raise NotImplementedError

    async def release_all(self) -> None:
        raise NotImplementedError

    def video_t(self) -> float:
        """Seconds since this session's recording started."""
        raise NotImplementedError


class HarnessBrowser:
    """Owns one Chromium instance; every session gets its own context."""

    def __init__(self, options: BrowserOptions) -> None:
        self._options = options

    async def __aenter__(self) -> Self:
        """Launch Chromium."""
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close Chromium and every open context."""
        raise NotImplementedError

    def session(
        self, controls: ControlScheme, *, record_video_to: Path | None = None
    ) -> AbstractAsyncContextManager[GameSession]:
        """Open an isolated context with one page, driven through ``controls``.

        When recording, closing the context finalises the video and moves it to
        ``record_video_to``.
        """
        raise NotImplementedError
