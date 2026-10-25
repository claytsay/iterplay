"""Loopback-only static file serving for game builds and the viewer (HLD §2, §9).

Generated game code is untrusted, so these servers must never be reachable from the venue
network. They always bind to 127.0.0.1, and the host is deliberately not configurable.
"""

import logging
import mimetypes
import os
import re
import socket
import socketserver
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources.abc import Traversable
from io import BufferedIOBase
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, BinaryIO, Final, Self
from urllib.parse import quote, unquote

LOOPBACK_HOST: Final = "127.0.0.1"

_log = logging.getLogger(__name__)

_MAX_PORT: Final = 65535
_SERVED_METHODS: Final = ("GET", "HEAD")

_ALLOWED_HOST: Final = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", re.IGNORECASE)
"""``Host`` headers that are answered, so a page on a DNS-rebound domain cannot read ours."""

_UNSAFE_CHARS: Final = re.compile(r"[\x00-\x1f\x7f\\]")
"""Characters never served in a path segment: control characters such as NUL, and backslashes."""

_BYTE_RANGE: Final = re.compile(r"bytes=([0-9]*)-([0-9]*)", re.IGNORECASE)

_CONTENT_TYPES: Final[Mapping[str, str]] = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".md": "text/markdown",
    ".mjs": "text/javascript",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
"""Media types the games and the viewer depend on, whatever the platform's own table says."""

_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"
_DISCONNECTS: Final = (ConnectionError, TimeoutError)
"""What a client hanging up mid-request looks like; logged at debug level, not as errors."""

_QUERY_SAFE: Final = "!$%&'()*+,/:;=?@~"
_CHUNK_BYTES: Final = 64 * 1024
_POLL_INTERVAL_S: Final = 0.1
_CONNECTION_TIMEOUT_S: Final = 30.0
_LOG_ESCAPES: Final = str.maketrans(
    {char: f"\\x{char:02x}" for char in (*range(0x20), *range(0x7F, 0xA0))} | {ord("\\"): "\\\\"}
)


@dataclass(frozen=True, slots=True)
class Mount:
    """Serve the files in ``directory`` under the URL prefix ``url_prefix``.

    Raises:
        ValueError: ``url_prefix`` does not start and end with "/" or has a hidden segment, a
            suffix lacks its leading dot, or ``index_file`` is not a plain file name.
    """

    url_prefix: str
    """``"/"``, or a path such as ``"/runs/"``."""

    directory: Path

    allowed_suffixes: frozenset[str] | None = None
    """If set, only files with these suffixes are served, e.g. ``{".json", ".mp4"}``. Case is
    ignored."""

    index_file: str | None = "index.html"
    """Served for a request naming a directory, since directories are never listed. When None,
    directory requests are not found."""

    def __post_init__(self) -> None:
        prefix = self.url_prefix
        if not (prefix.startswith("/") and prefix.endswith("/")) or not all(
            map(_is_servable, _segments(prefix))
        ):
            raise ValueError(f"url_prefix must look like '/' or '/name/', got {prefix!r}")
        suffixes = self.allowed_suffixes
        if suffixes is not None and not all(suffix.startswith(".") for suffix in suffixes):
            raise ValueError(f"allowed_suffixes must start with '.', got {sorted(suffixes)}")
        index = self.index_file
        if index is not None and not (index and "/" not in index and _is_servable(index)):
            raise ValueError(f"index_file must be a plain file name, got {index!r}")


class LoopbackHTTPServer:
    """A threaded ``http.server`` on 127.0.0.1 serving one or more mounts.

    Requests are confined to their mount's directory (no ``..`` or symlink escapes),
    directory listings and dotfiles are never served, and every response carries
    ``Cache-Control: no-store`` so a browser never shows a stale version of a game.

    Each request is answered from the mount with the longest matching URL prefix. Only GET
    and HEAD are served, single byte ranges are honoured so videos can seek, and requests
    naming a host other than 127.0.0.1 or localhost are refused (a DNS-rebinding guard). A
    server runs once: start it, then stop it.
    """

    def __init__(self, mounts: Sequence[Mount], *, port: int) -> None:
        """Prepare a server on ``port``; 0 lets the OS choose a free port at start.

        Raises:
            ValueError: ``port`` is not 0-65535, or two mounts share a URL prefix.
        """
        if not 0 <= port <= _MAX_PORT:
            raise ValueError(f"port must be between 0 and {_MAX_PORT}, got {port}")
        self._routes = _routes(mounts)
        self._port = port
        self._lock = threading.Lock()
        self._httpd: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>/``. With ``port=0``, the OS-assigned port once started."""
        _, port = self.server_address
        return f"http://{LOOPBACK_HOST}:{port}/"

    @property
    def server_address(self) -> tuple[str, int]:
        """The ``(host, port)`` the socket is bound to; until started, the requested port."""
        httpd = self._httpd
        if httpd is None:
            return LOOPBACK_HOST, self._port
        host, port = httpd.server_address[:2]
        return str(host), port

    def start(self) -> None:
        """Bind, then serve on a background daemon thread.

        Raises:
            RuntimeError: The server was already started.
            OSError: The port cannot be bound, for example because it is in use.
        """
        with self._lock:
            if self._httpd is not None:
                raise RuntimeError("This LoopbackHTTPServer was already started")
            handler = partial(_RequestHandler, routes=self._routes)
            httpd = _ThreadingServer((LOOPBACK_HOST, self._port), handler)
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": _POLL_INTERVAL_S},
                name=f"iterplay-http-{httpd.server_port}",
                daemon=True,
            )
            thread.start()
            self._httpd, self._thread = httpd, thread
        _log.debug("Serving %s: %s", self.base_url, ", ".join(map(str, self._routes)))

    def stop(self) -> None:
        """Stop serving and close the socket. Does nothing unless the server is running."""
        with self._lock:
            httpd, thread = self._httpd, self._thread
            if httpd is None or thread is None or self._stopped:
                return
            self._stopped = True
            httpd.shutdown()
            thread.join()
            httpd.server_close()
        _log.debug("Stopped serving %s", self.base_url)

    def serve_forever(self) -> None:
        """Bind, then serve until interrupted (Ctrl-C), blocking the calling thread.

        Returns once the socket is closed, also when another thread calls :meth:`stop`.

        Raises:
            RuntimeError: The server was already started.
            OSError: The port cannot be bound, for example because it is in use.
        """
        self.start()
        try:
            self._wait()
        except KeyboardInterrupt:
            _log.debug("Interrupted while serving %s", self.base_url)
        finally:
            self.stop()

    def __enter__(self) -> Self:
        """Start serving in the background."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop serving."""
        self.stop()

    def _wait(self) -> None:
        """Block until the serving thread ends, waking regularly so Ctrl-C is noticed."""
        thread = self._thread
        while thread is not None and thread.is_alive():
            thread.join(_POLL_INTERVAL_S)


def resource_dir(resource: Traversable) -> Path:
    """The directory on disk behind a package resource, so that it can be mounted.

    Raises:
        RuntimeError: ``resource`` is not a directory on the filesystem, for example because
            the package was imported from a zip archive.
    """
    if not isinstance(resource, Path) or not resource.is_dir():
        raise RuntimeError(f"Package resource {resource} is not a directory on disk")
    return resource


@dataclass(frozen=True, slots=True)
class _Route:
    """A mount, prepared for matching requests against it."""

    url_prefix: str
    prefix: tuple[str, ...]
    root: Path
    suffixes: frozenset[str] | None
    """Lower-cased."""

    index_file: str | None

    @classmethod
    def of(cls, mount: Mount) -> Self:
        suffixes = mount.allowed_suffixes
        return cls(
            url_prefix=mount.url_prefix,
            prefix=_segments(mount.url_prefix),
            root=mount.directory.absolute(),
            suffixes=None if suffixes is None else frozenset(s.lower() for s in suffixes),
            index_file=mount.index_file,
        )

    def __str__(self) -> str:
        return f"{self.url_prefix} -> {self.root}"

    def remainder(self, segments: tuple[str, ...]) -> tuple[str, ...] | None:
        """The part of ``segments`` below this route's prefix, or None if not under it."""
        depth = len(self.prefix)
        return segments[depth:] if segments[:depth] == self.prefix else None

    def allows(self, name: str) -> bool:
        return self.suffixes is None or PurePosixPath(name).suffix.lower() in self.suffixes


@dataclass(frozen=True, slots=True)
class _File:
    """A file that passed every check, ready to be served."""

    path: Path
    """With symlinks resolved."""

    is_index: bool
    """The request named a directory, and this is its index file."""


class _UnsatisfiableRangeError(Exception):
    """A ``Range`` header asks only for bytes past the end of the file."""


class _RequestHandler(BaseHTTPRequestHandler):
    """Answers GET and HEAD from the server's mounts; any other method gets 405.

    Each connection carries one request (HTTP/1.0), so a browser never reuses a connection
    to a stopped server, such as the previous version's server on the same port.
    """

    server_version = "iterplay"
    timeout = _CONNECTION_TIMEOUT_S
    disable_nagle_algorithm = True

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        *,
        routes: tuple[_Route, ...],
    ) -> None:
        self._routes = routes
        super().__init__(request, client_address, server)

    def parse_request(self) -> bool:
        """Parse as usual, then refuse foreign ``Host`` headers and unserved methods."""
        if not super().parse_request():
            return False
        if not self._host_allowed():
            self._send_status(HTTPStatus.FORBIDDEN)
            return False
        if self.command not in _SERVED_METHODS:
            allow = ", ".join(_SERVED_METHODS)
            self._send_status(HTTPStatus.METHOD_NOT_ALLOWED, {"Allow": allow})
            return False
        return True

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        _log.debug("%s %s", self.address_string(), message.translate(_LOG_ESCAPES))

    def version_string(self) -> str:
        return self.server_version

    def _host_allowed(self) -> bool:
        host = str(self.headers.get("Host", "")).strip()
        return not host or _ALLOWED_HOST.fullmatch(host) is not None

    def _serve(self, *, send_body: bool) -> None:
        try:
            self._respond(send_body=send_body)
        except _DISCONNECTS as error:
            self.close_connection = True
            _log.debug("%s disconnected: %s", self.address_string(), error)

    def _respond(self, *, send_body: bool) -> None:
        path, _, query = self.path.partition("#")[0].partition("?")
        segments = _request_segments(path)
        found = None if segments is None else self._find(segments)
        wants_directory = path.endswith("/")
        if segments is None or found is None or (wants_directory and not found.is_index):
            self._send_status(HTTPStatus.NOT_FOUND)
        elif found.is_index and not wants_directory:
            location = _directory_url(segments, query)
            self._send_status(HTTPStatus.MOVED_PERMANENTLY, {"Location": location})
        else:
            self._send_file(found.path, send_body=send_body)

    def _find(self, segments: tuple[str, ...]) -> _File | None:
        """Look ``segments`` up in the mount with the longest matching URL prefix."""
        for route in self._routes:
            remainder = route.remainder(segments)
            if remainder is not None:
                return _locate(route, remainder)
        return None

    def _send_file(self, path: Path, *, send_body: bool) -> None:
        try:
            file = path.open("rb")
        except OSError:
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        with file:
            size = os.fstat(file.fileno()).st_size
            try:
                byte_range = self._byte_range(size)
            except _UnsatisfiableRangeError:
                unsatisfiable = {"Content-Range": f"bytes */{size}"}
                self._send_status(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, unsatisfiable)
                return
            start, stop = (0, size) if byte_range is None else byte_range
            self.send_response(HTTPStatus.OK if byte_range is None else HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", _content_type(path.name))
            self.send_header("Content-Length", str(stop - start))
            self.send_header("Accept-Ranges", "bytes")
            if byte_range is not None:
                self.send_header("Content-Range", f"bytes {start}-{stop - 1}/{size}")
            self.end_headers()
            if send_body:
                _copy(file, self.wfile, start, stop - start)

    def _byte_range(self, size: int) -> tuple[int, int] | None:
        """The byte range a GET asks for, or None to send the whole file.

        Range is only defined for GET. A conditional ``If-Range`` cannot match, because no
        validators are sent, so it also gets the whole file.

        Raises:
            _UnsatisfiableRangeError: The range starts at or past the end of the file.
        """
        header = self.headers.get("Range")
        if self.command != "GET" or header is None or "If-Range" in self.headers:
            return None
        return _parse_byte_range(str(header), size)

    def _send_status(self, status: HTTPStatus, headers: Mapping[str, str] | None = None) -> None:
        """Answer with ``status``, ``headers`` and a one-line text body (none for HEAD)."""
        body = f"{status.value} {status.phrase}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _ThreadingServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` without its reverse DNS lookup, logging errors, not printing."""

    request_queue_size = 64
    """Browsers open several connections at once, e.g. one per game module."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = LOOPBACK_HOST
        self.server_port = self.server_address[1]

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exception()
        if isinstance(error, _DISCONNECTS):
            _log.debug("%s disconnected: %s", client_address, error)
        else:
            _log.exception("Error serving a request from %s", client_address)


def _routes(mounts: Sequence[Mount]) -> tuple[_Route, ...]:
    """Prepare ``mounts`` for matching, longest URL prefix first.

    Raises:
        ValueError: Two mounts share a URL prefix.
    """
    routes = sorted(map(_Route.of, mounts), key=lambda route: len(route.prefix), reverse=True)
    seen: set[tuple[str, ...]] = set()
    for route in routes:
        if route.prefix in seen:
            raise ValueError(f"More than one mount at {route.url_prefix!r}")
        seen.add(route.prefix)
    return tuple(routes)


def _segments(path: str) -> tuple[str, ...]:
    """The segments of a decoded path, without empty and ``.`` segments."""
    return tuple(segment for segment in path.split("/") if segment not in ("", "."))


def _is_servable(name: str) -> bool:
    """False for ``..``, dotfiles, and names containing a backslash or control character."""
    return not name.startswith(".") and _UNSAFE_CHARS.search(name) is None


def _request_segments(path: str) -> tuple[str, ...] | None:
    """The percent-decoded segments of a request path, or None if it may not be served.

    Repeated slashes and ``.`` segments are normalised away, but ``..`` is refused rather than
    resolved, and so are dotfiles, backslashes and control characters such as NUL.
    """
    if not path.startswith("/"):
        return None
    try:
        segments = _segments(unquote(path, errors="strict"))
    except UnicodeDecodeError:
        return None
    return segments if all(map(_is_servable, segments)) else None


def _locate(route: _Route, segments: tuple[str, ...]) -> _File | None:
    """The file ``segments`` name under ``route``, or None if it may not be served."""
    try:
        return _probe(route, segments)
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop, before Python 3.13
        return None


def _probe(route: _Route, segments: tuple[str, ...]) -> _File | None:
    """Look ``segments`` up like :func:`_locate`, raising ``OSError`` if a path is missing.

    A directory stands for its index file. With symlinks resolved, the file must still be
    inside the mount and no part of its path below the mount may be hidden. Both the
    requested and the resolved name must have an allowed suffix.
    """
    root = route.root.resolve(strict=True)
    requested = root.joinpath(*segments)
    is_index = requested.is_dir()
    if is_index:
        if route.index_file is None:
            return None
        requested /= route.index_file
    target = requested.resolve(strict=True)
    if not (root.is_dir() and target.is_relative_to(root) and target.is_file()):
        return None
    if not all(map(_is_servable, target.relative_to(root).parts)):
        return None
    if not (route.allows(requested.name) and route.allows(target.name)):
        return None
    return _File(target, is_index)


def _directory_url(segments: tuple[str, ...], query: str) -> str:
    """The URL of the directory that ``segments`` name, with its trailing slash."""
    path = "/" + "".join(f"{quote(segment)}/" for segment in segments)
    return f"{path}?{quote(query, safe=_QUERY_SAFE)}" if query else path


def _parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The ``(start, stop)`` offsets of the single byte range in a ``Range`` header.

    Returns None, so the whole file is sent, for anything but one ``bytes`` range: RFC 9110
    lets a server ignore multiple ranges and malformed headers.

    Raises:
        _UnsatisfiableRangeError: The range starts at or past the end of the file.
    """
    match = _BYTE_RANGE.fullmatch(header.strip())
    if match is None:
        return None
    try:
        first, last = (int(digits) if digits else None for digits in match.groups())
    except ValueError:  # more digits than int() converts
        return None
    if first is None:
        if last is None:
            return None
        start, stop = max(size - last, 0), size
    elif last is not None and last < first:
        return None
    else:
        start = first
        stop = size if last is None else min(last + 1, size)
    if start >= stop:
        raise _UnsatisfiableRangeError
    return start, stop


def _content_type(name: str) -> str:
    """The ``Content-Type`` for a file name. Text is declared to be UTF-8."""
    suffix = PurePosixPath(name).suffix.lower()
    media_type = _CONTENT_TYPES.get(suffix) or _builtin_types().get(suffix, _FALLBACK_CONTENT_TYPE)
    return f"{media_type}; charset=utf-8" if media_type.startswith("text/") else media_type


@cache
def _builtin_types() -> Mapping[str, str]:
    """Python's suffix table, without the platform's ``mime.types`` additions."""
    _, strict = mimetypes.MimeTypes().types_map
    return strict


def _copy(source: BinaryIO, sink: BufferedIOBase, start: int, length: int) -> None:
    """Write ``length`` bytes of ``source``, from offset ``start``, to ``sink``."""
    source.seek(start)
    remaining = length
    while remaining > 0:
        chunk = source.read(min(remaining, _CHUNK_BYTES))
        if not chunk:  # the file shrank after its length was sent
            return
        sink.write(chunk)
        remaining -= len(chunk)
