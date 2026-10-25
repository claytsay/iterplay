"""Loopback static serving (HLD §2, §9): confinement, headers, byte ranges and lifecycle, and
the game and viewer servers built on it. Every test talks to a real server on port 0."""

import _thread
import http.client
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pytest
from playwright.sync_api import ConsoleMessage, sync_playwright

from iterplay.harness.server import game_server
from iterplay.serving import LOOPBACK_HOST, LoopbackHTTPServer, Mount, resource_dir
from iterplay.storage.game_files import VENDOR_ASSETS
from iterplay.viewer.server import ARTIFACT_SUFFIXES, viewer_server

REFERENCE_GAME = Path(__file__).resolve().parents[1] / "examples" / "reference_game"
VIEWER_PAGE = resource_dir(files("iterplay.viewer") / "static") / "viewer.html"

DIGITS = b"0123456789"
BIG = bytes(range(256)) * 1200
"""300 KiB: several of the server's copy chunks."""

SECRET = b"outside the mount"

SITE_FILES: Mapping[str, bytes] = {
    "index.html": b"<!doctype html><title>home</title>",
    "main.js": b"export const answer = 42;\n",
    "module.mjs": b"export default 1;\n",
    "style.css": b"body { margin: 0; }\n",
    "data.json": b'{"ok": true}\n',
    "UPPER.JSON": b"[]\n",
    "notes.md": b"# Notes\n",
    "clip.webm": DIGITS,
    "clip.mp4": DIGITS,
    "image.png": b"\x89PNG\r\n\x1a\n",
    "blob.xyz123": b"?",
    "LICENSE": b"MIT\n",
    "empty.webm": b"",
    "big.bin": BIG,
    "with space.js": b"// space\n",
    "café.js": b"// accent\n",
    "sub/index.html": b"<!doctype html><title>sub</title>",
    "sub/page.js": b"// page\n",
    "dir with space/index.html": b"<!doctype html><title>spaced</title>",
    "assets/a.js": b"// a\n",
    "assets/b.js": b"// b\n",
    ".env": b"dotfile\n",
    ".hidden/secret.js": b"// hidden\n",
    "sub/.git/config": b"[core]\n",
}

RUN_FILES: Mapping[str, bytes] = {
    "index.json": b'{"runs": ["r1"]}',
    "r1/summary.json": b'{"best_version": 1}',
    "r1/config.json": b"{}",
    "r1/design_doc.md": b"# Tunnel Turbulence\n",
    "r1/.notes.json": b"{}",
    "r1/v1/score.json": b'{"total": 0.35}',
    "r1/v1/critique.verified.json": b'{"confirmed": []}',
    "r1/v1/changelog.md": b"- I3: register wall colliders\n",
    "r1/v1/playtest/random.webm": DIGITS,
    "r1/v1/playtest/combined_360p.mp4": DIGITS,
    "r1/v1/playtest/random.telemetry.jsonl": b"{}\n",
    "r1/v1/playtest/console.log": b"random: ok\n",
    "r1/v1/game/index.html": b"<!doctype html>",
    "r1/v1/game/main.js": b"// generated\n",
    "r1/v1/game/vendor/three.module.js": b"// three\n",
}

SEEK_SCRIPT = """async ([src, time]) => {
  const video = document.createElement("video");
  video.muted = true;
  video.preload = "auto";
  const event = (name) => new Promise((resolve, reject) => {
    video.addEventListener(name, resolve, { once: true });
    video.addEventListener("error", () => reject(new Error(`cannot load ${src}`)), { once: true });
    setTimeout(() => reject(new Error(`no ${name} event`)), 15000);
  });
  const loaded = event("loadedmetadata");
  video.src = src;
  document.body.append(video);
  await loaded;
  const seeked = event("seeked");
  video.currentTime = time;
  await seeked;
  return video.currentTime;
}"""


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: http.client.HTTPMessage
    body: bytes


def fetch(
    server: LoopbackHTTPServer,
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
) -> Reply:
    """Send one request with ``path`` exactly as written: http.client does not normalise it."""
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, headers=dict(headers or {}))
        response = connection.getresponse()
        return Reply(response.status, response.headers, response.read())
    finally:
        connection.close()


def send_raw(server: LoopbackHTTPServer, request: bytes) -> bytes:
    """Send ``request`` verbatim and read until the server closes the connection."""
    with socket.create_connection(server.server_address, timeout=10) as connection:
        connection.sendall(request)
        chunks: list[bytes] = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def wait_until_bound(server: LoopbackHTTPServer, timeout_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while server.server_address[1] == 0:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def write_tree(root: Path, contents: Mapping[str, bytes]) -> Path:
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def write_game(game_dir: Path) -> Path:
    return write_tree(
        game_dir,
        {
            "index.html": b'<!doctype html><script type="module" src="./main.js"></script>',
            "main.js": b"window.started = true;\n",
            "level.json": b'{"tiles": []}',
            "sprite.png": b"\x89PNG\r\n\x1a\n",
        },
    )


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``SITE_FILES``, symlinks in and out of it, and a secret next to it."""
    base = tmp_path_factory.mktemp("serving")
    outside = write_tree(base / "outside", {"secret.txt": SECRET, "index.html": SECRET})
    root = write_tree(base / "site", SITE_FILES)
    links = {
        "escape.txt": outside / "secret.txt",
        "linked": outside,
        "alias.js": Path("main.js"),
        "alias.json": Path("main.js"),
        "peek.js": Path(".hidden") / "secret.js",
        "loop.js": Path("loop.js"),
        "subalias": Path("sub"),
    }
    for name, target in links.items():
        (root / name).symlink_to(target)
    return root


@pytest.fixture(scope="module")
def server(site: Path) -> Iterator[LoopbackHTTPServer]:
    with LoopbackHTTPServer([Mount("/", site)], port=0) as running:
        yield running


@pytest.fixture(scope="module")
def media_server(site: Path) -> Iterator[LoopbackHTTPServer]:
    mount = Mount("/", site, allowed_suffixes=frozenset({".JSON", ".webm"}))
    with LoopbackHTTPServer([mount], port=0) as running:
        yield running


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_tree(tmp_path_factory.mktemp("viewer") / "runs", RUN_FILES)


@pytest.fixture(scope="module")
def viewer(runs_dir: Path) -> Iterator[LoopbackHTTPServer]:
    with viewer_server(runs_dir, port=0) as running:
        yield running


# Binding and lifecycle


def test_binds_loopback_on_the_port_the_os_assigns(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)
    assert server.server_address == (LOOPBACK_HOST, 0)
    assert server.base_url == "http://127.0.0.1:0/"

    with server:
        host, port = server.server_address
        with urllib.request.urlopen(f"{server.base_url}main.js", timeout=10) as response:
            body = response.read()

    assert host == "127.0.0.1"
    assert port != 0
    assert server.base_url == f"http://127.0.0.1:{port}/"
    assert body == SITE_FILES["main.js"]


def test_base_url_names_the_requested_port_before_starting(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=8737)

    assert server.server_address == ("127.0.0.1", 8737)
    assert server.base_url == "http://127.0.0.1:8737/"


def test_a_server_starts_only_once(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)
    with server:
        with pytest.raises(RuntimeError):
            server.start()
        with pytest.raises(RuntimeError):
            server.serve_forever()
        assert fetch(server, "/main.js").status == 200

    with pytest.raises(RuntimeError):
        server.start()


def test_stop_is_idempotent_and_frees_the_port(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)
    server.stop()
    server.start()
    port = server.server_address[1]
    assert fetch(server, "/main.js").status == 200

    server.stop()
    server.stop()

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((LOOPBACK_HOST, port), timeout=5).close()
    with LoopbackHTTPServer([Mount("/", site)], port=port) as successor:
        assert fetch(successor, "/main.js").status == 200


def test_a_taken_port_fails_to_start(site: Path, server: LoopbackHTTPServer) -> None:
    rival = LoopbackHTTPServer([Mount("/", site)], port=server.server_address[1])

    with pytest.raises(OSError):
        rival.start()

    rival.stop()
    assert fetch(server, "/main.js").status == 200


def test_serve_forever_returns_after_ctrl_c_and_closes_the_socket(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)
    statuses: list[int] = []

    def press_ctrl_c_once_serving() -> None:
        if not wait_until_bound(server):
            return
        try:
            statuses.append(fetch(server, "/main.js").status)
            time.sleep(0.2)
        finally:
            _thread.interrupt_main()

    helper = threading.Thread(target=press_ctrl_c_once_serving)
    helper.start()
    server.serve_forever()
    helper.join()

    assert statuses == [200]
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(server.server_address, timeout=5).close()


def test_the_context_manager_stops_when_its_body_fails(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)

    with pytest.raises(LookupError), server:
        raise LookupError

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(server.server_address, timeout=5).close()


def test_stop_from_another_thread_ends_serve_forever(site: Path) -> None:
    server = LoopbackHTTPServer([Mount("/", site)], port=0)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    assert wait_until_bound(server)
    assert fetch(server, "/main.js").status == 200

    server.stop()
    serving.join(timeout=5)

    assert not serving.is_alive()


@pytest.mark.parametrize("port", [-1, 65536])
def test_the_port_must_be_valid(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        LoopbackHTTPServer([Mount("/", tmp_path)], port=port)


def test_serves_concurrent_requests(server: LoopbackHTTPServer) -> None:
    with ThreadPoolExecutor(max_workers=16) as pool:
        replies = list(pool.map(lambda _: fetch(server, "/big.bin"), range(32)))

    assert all(reply.status == 200 and reply.body == BIG for reply in replies)


# Mounts


@pytest.mark.parametrize(
    ("url_prefix", "allowed_suffixes", "index_file"),
    [
        ("runs/", None, "index.html"),
        ("/runs", None, "index.html"),
        ("", None, "index.html"),
        ("/../", None, "index.html"),
        ("/.git/", None, "index.html"),
        ("/a\\b/", None, "index.html"),
        ("/", frozenset({"json"}), "index.html"),
        ("/", None, ""),
        ("/", None, "sub/index.html"),
        ("/", None, ".index.html"),
    ],
)
def test_invalid_mounts_are_rejected(
    tmp_path: Path,
    url_prefix: str,
    allowed_suffixes: frozenset[str] | None,
    index_file: str | None,
) -> None:
    with pytest.raises(ValueError):
        Mount(url_prefix, tmp_path, allowed_suffixes, index_file)


def test_mounts_need_distinct_prefixes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="/runs/"):
        LoopbackHTTPServer([Mount("/runs/", tmp_path), Mount("/runs/", tmp_path / "b")], port=0)


def test_the_longest_matching_prefix_wins(tmp_path: Path) -> None:
    for name in ("top", "assets", "images", "top/assetsX"):
        write_tree(tmp_path / name, {"which.txt": name.encode()})
    (tmp_path / "top" / "only-top.txt").write_text("top")
    mounts = [
        Mount("/", tmp_path / "top"),
        Mount("/assets/img/", tmp_path / "images"),
        Mount("/assets/", tmp_path / "assets"),
    ]

    with LoopbackHTTPServer(mounts, port=0) as server:
        bodies = {
            path: fetch(server, path).body
            for path in ("/which.txt", "/assets/which.txt", "/assets/img/which.txt")
        }
        lookalike = fetch(server, "/assetsX/which.txt")
        no_fallback = fetch(server, "/assets/only-top.txt")

    assert bodies == {
        "/which.txt": b"top",
        "/assets/which.txt": b"assets",
        "/assets/img/which.txt": b"images",
    }
    assert lookalike.body == b"top/assetsX"
    assert no_fallback.status == 404


def test_a_missing_mount_directory_serves_nothing(tmp_path: Path) -> None:
    with LoopbackHTTPServer([Mount("/", tmp_path / "absent")], port=0) as server:
        assert fetch(server, "/").status == 404
        assert fetch(server, "/main.js").status == 404


def test_a_relative_mount_directory_is_pinned_when_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_tree(tmp_path / "site", {"main.js": b"// pinned\n"})
    monkeypatch.chdir(tmp_path)
    server = LoopbackHTTPServer([Mount("/", Path("site"))], port=0)
    monkeypatch.chdir(tmp_path / "site")

    with server:
        reply = fetch(server, "/main.js")

    assert reply.body == b"// pinned\n"


def test_resource_dir_needs_a_directory_on_disk(tmp_path: Path) -> None:
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("package/static/viewer.html", "<!doctype html>")

    assert resource_dir(VENDOR_ASSETS).joinpath("three.module.js").is_file()
    with pytest.raises(RuntimeError):
        resource_dir(zipfile.Path(archive, "package/static/"))
    with pytest.raises(RuntimeError):
        resource_dir(tmp_path / "absent")


# Responses


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/index.html", "text/html; charset=utf-8"),
        ("/main.js", "text/javascript; charset=utf-8"),
        ("/module.mjs", "text/javascript; charset=utf-8"),
        ("/style.css", "text/css; charset=utf-8"),
        ("/data.json", "application/json"),
        ("/notes.md", "text/markdown; charset=utf-8"),
        ("/clip.webm", "video/webm"),
        ("/clip.mp4", "video/mp4"),
        ("/image.png", "image/png"),
        ("/blob.xyz123", "application/octet-stream"),
        ("/LICENSE", "application/octet-stream"),
    ],
)
def test_sends_the_file_with_its_type_and_length(
    server: LoopbackHTTPServer, path: str, content_type: str
) -> None:
    reply = fetch(server, path)

    assert reply.status == 200
    assert reply.body == SITE_FILES[path.removeprefix("/")]
    assert reply.headers["Content-Type"] == content_type
    assert reply.headers["Content-Length"] == str(len(reply.body))
    assert reply.headers["Accept-Ranges"] == "bytes"


@pytest.mark.parametrize(
    ("path", "method", "headers", "status"),
    [
        ("/main.js", "GET", {}, 200),
        ("/main.js", "HEAD", {}, 200),
        ("/clip.webm", "GET", {"Range": "bytes=0-1"}, 206),
        ("/sub", "GET", {}, 301),
        ("/main.js", "GET", {"Host": "evil.example"}, 403),
        ("/missing.js", "GET", {}, 404),
        ("/main.js", "POST", {}, 405),
        ("/clip.webm", "GET", {"Range": "bytes=50-"}, 416),
    ],
)
def test_every_response_forbids_caching_and_sniffing(
    server: LoopbackHTTPServer, path: str, method: str, headers: dict[str, str], status: int
) -> None:
    reply = fetch(server, path, method=method, headers=headers)

    assert reply.status == status
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.headers["X-Content-Type-Options"] == "nosniff"


def test_malformed_requests_also_forbid_caching(server: LoopbackHTTPServer) -> None:
    reply = send_raw(server, b"NONSENSE\r\n\r\n")

    assert reply.startswith(b"HTTP/1.0 400 ")
    assert b"\r\nCache-Control: no-store\r\n" in reply
    assert b"\r\nX-Content-Type-Options: nosniff\r\n" in reply


@pytest.mark.parametrize(("path", "status"), [("/main.js", 200), ("/missing.js", 404)])
def test_head_sends_the_headers_alone(server: LoopbackHTTPServer, path: str, status: int) -> None:
    reply = send_raw(server, f"HEAD {path} HTTP/1.0\r\n\r\n".encode())
    head, _, body = reply.partition(b"\r\n\r\n")

    assert head.startswith(f"HTTP/1.0 {status} ".encode())
    if status == 200:
        assert f"\r\nContent-Length: {len(SITE_FILES['main.js'])}".encode() in head
    assert body == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "BREW"])
def test_only_get_and_head_are_served(server: LoopbackHTTPServer, method: str) -> None:
    reply = fetch(server, "/main.js", method=method)

    assert reply.status == 405
    assert reply.headers["Allow"] == "GET, HEAD"
    assert SITE_FILES["main.js"] not in reply.body


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:8737", "127.0.0.1.evil.example", "localhost.evil", "[::1]:80"],
)
def test_foreign_host_headers_are_refused(server: LoopbackHTTPServer, host: str) -> None:
    reply = fetch(server, "/main.js", headers={"Host": host})

    assert reply.status == 403
    assert SITE_FILES["main.js"] not in reply.body


@pytest.mark.parametrize("host", ["127.0.0.1", "LOCALHOST", "localhost:8738"])
def test_loopback_host_headers_are_served(server: LoopbackHTTPServer, host: str) -> None:
    assert fetch(server, "/main.js", headers={"Host": host}).status == 200


def test_a_request_without_a_host_header_is_served(server: LoopbackHTTPServer) -> None:
    reply = send_raw(server, b"GET /main.js HTTP/1.0\r\n\r\n")

    assert reply.startswith(b"HTTP/1.0 200 ")
    assert reply.endswith(SITE_FILES["main.js"])


# Confinement


@pytest.mark.parametrize(
    "path",
    [
        "/../outside/secret.txt",
        "/sub/../../outside/secret.txt",
        "/%2e%2e/outside/secret.txt",
        "/%2E%2E%2Foutside%2Fsecret.txt",
        "/sub/..%2F..%2Foutside%2Fsecret.txt",
        "/.%2e/outside/secret.txt",
        "/%252e%252e/outside/secret.txt",
        "/..%5Coutside%5Csecret.txt",
        "/sub\\..\\..\\outside\\secret.txt",
        "outside/secret.txt",
        "/main.js%00",
        "/main.js%00.html",
        "/escape.txt",
        "/linked/secret.txt",
        "/linked/",
        "/linked",
        "/loop.js",
        "/%ff.js",
    ],
)
def test_requests_cannot_leave_the_mount(server: LoopbackHTTPServer, path: str) -> None:
    reply = fetch(server, path)

    assert reply.status == 404
    assert SECRET not in reply.body


@pytest.mark.parametrize(
    "path", ["/.env", "/%2eenv", "/.hidden/secret.js", "/.hidden/", "/sub/.git/config", "/peek.js"]
)
def test_dotfiles_are_never_served(server: LoopbackHTTPServer, path: str) -> None:
    assert fetch(server, path).status == 404


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/alias.js", "main.js"),
        ("/subalias/", "sub/index.html"),
        ("/subalias/page.js", "sub/page.js"),
    ],
)
def test_symlinks_within_the_mount_are_followed(
    server: LoopbackHTTPServer, path: str, name: str
) -> None:
    reply = fetch(server, path)

    assert reply.status == 200
    assert reply.body == SITE_FILES[name]


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/with%20space.js", "with space.js"),
        ("/caf%C3%A9.js", "café.js"),
        ("/%6Dain.js", "main.js"),
        ("//main.js", "main.js"),
        ("/sub//page.js", "sub/page.js"),
        ("/./main.js", "main.js"),
        ("/main.js?v=2#top", "main.js"),
    ],
)
def test_paths_are_percent_decoded_and_normalised(
    server: LoopbackHTTPServer, path: str, name: str
) -> None:
    reply = fetch(server, path)

    assert reply.status == 200
    assert reply.body == SITE_FILES[name]


def test_a_file_with_a_trailing_slash_is_not_found(server: LoopbackHTTPServer) -> None:
    assert fetch(server, "/main.js/").status == 404


@pytest.mark.parametrize("path", ["/" + "a" * 300 + ".js", "/a" * 900 + ".js"])
def test_overlong_paths_are_not_found(
    server: LoopbackHTTPServer, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    assert fetch(server, path).status == 404
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_special_files_are_never_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.js")

    with LoopbackHTTPServer([Mount("/", tmp_path)], port=0) as server:
        assert fetch(server, "/pipe.js").status == 404


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads anything")
def test_unreadable_files_are_not_found(tmp_path: Path) -> None:
    locked = tmp_path / "locked.js"
    locked.write_text("// locked\n")
    locked.chmod(0)

    try:
        with LoopbackHTTPServer([Mount("/", tmp_path)], port=0) as server:
            reply = fetch(server, "/locked.js")
    finally:
        locked.chmod(0o600)

    assert reply.status == 404


# Directories


@pytest.mark.parametrize("path", ["/assets/", "/assets"])
def test_directories_are_never_listed(server: LoopbackHTTPServer, path: str) -> None:
    reply = fetch(server, path)

    assert reply.status == 404
    assert b"a.js" not in reply.body


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/", "index.html"),
        ("/?seed=3", "index.html"),
        ("/sub/", "sub/index.html"),
        ("/dir%20with%20space/", "dir with space/index.html"),
    ],
)
def test_a_directory_serves_its_index_file(
    server: LoopbackHTTPServer, path: str, name: str
) -> None:
    reply = fetch(server, path)

    assert reply.status == 200
    assert reply.body == SITE_FILES[name]
    assert reply.headers["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/sub", "/sub/"),
        ("/sub?seed=3&bot=newbie", "/sub/?seed=3&bot=newbie"),
        ("/sub/.", "/sub/"),
        ("/dir%20with%20space", "/dir%20with%20space/"),
    ],
)
def test_a_directory_without_its_slash_redirects(
    server: LoopbackHTTPServer, path: str, location: str
) -> None:
    reply = fetch(server, path)

    assert reply.status == 301
    assert reply.headers["Location"] == location


def test_the_redirect_leads_to_the_index_file(server: LoopbackHTTPServer) -> None:
    with urllib.request.urlopen(f"{server.base_url}sub", timeout=10) as response:
        assert response.geturl() == f"{server.base_url}sub/"
        assert response.read() == SITE_FILES["sub/index.html"]


def test_a_custom_index_file(site: Path) -> None:
    with LoopbackHTTPServer([Mount("/", site, index_file="notes.md")], port=0) as server:
        reply = fetch(server, "/")

    assert reply.body == SITE_FILES["notes.md"]
    assert reply.headers["Content-Type"] == "text/markdown; charset=utf-8"


def test_without_an_index_file_directories_are_not_found(site: Path) -> None:
    with LoopbackHTTPServer([Mount("/", site, index_file=None)], port=0) as server:
        statuses = [fetch(server, path).status for path in ("/", "/sub/", "/sub", "/main.js")]

    assert statuses == [404, 404, 404, 200]


# Suffix allowlist


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/data.json", 200),
        ("/UPPER.JSON", 200),
        ("/clip.webm", 200),
        ("/main.js", 404),
        ("/notes.md", 404),
        ("/LICENSE", 404),
        ("/", 404),
        ("/sub/", 404),
        ("/alias.json", 404),
    ],
)
def test_only_allowed_suffixes_are_served(
    media_server: LoopbackHTTPServer, path: str, status: int
) -> None:
    assert fetch(media_server, path).status == status


# Byte ranges


@pytest.mark.parametrize(
    ("range_header", "body", "content_range"),
    [
        ("bytes=0-3", b"0123", "bytes 0-3/10"),
        ("bytes=5-", b"56789", "bytes 5-9/10"),
        ("bytes=-4", b"6789", "bytes 6-9/10"),
        ("bytes=-20", DIGITS, "bytes 0-9/10"),
        ("bytes=8-100", b"89", "bytes 8-9/10"),
        ("bytes=9-9", b"9", "bytes 9-9/10"),
        ("Bytes=1-2", b"12", "bytes 1-2/10"),
        ("bytes=1-2 ", b"12", "bytes 1-2/10"),
    ],
)
def test_a_single_byte_range_gets_206(
    server: LoopbackHTTPServer, range_header: str, body: bytes, content_range: str
) -> None:
    reply = fetch(server, "/clip.webm", headers={"Range": range_header})

    assert reply.status == 206
    assert reply.body == body
    assert reply.headers["Content-Range"] == content_range
    assert reply.headers["Content-Length"] == str(len(body))
    assert reply.headers["Content-Type"] == "video/webm"


@pytest.mark.parametrize("range_header", ["bytes=10-", "bytes=10-20", "bytes=-0"])
def test_an_unsatisfiable_range_gets_416(server: LoopbackHTTPServer, range_header: str) -> None:
    reply = fetch(server, "/clip.webm", headers={"Range": range_header})

    assert reply.status == 416
    assert reply.headers["Content-Range"] == "bytes */10"
    assert DIGITS not in reply.body


@pytest.mark.parametrize(
    "range_header",
    [
        "bytes=4-2",
        "bytes=0-1,4-5",
        "items=0-1",
        "bytes=abc",
        "bytes=-",
        "bytes 0-1",
        "bytes=" + "9" * 5000 + "-",
    ],
)
def test_other_range_headers_get_the_whole_file(
    server: LoopbackHTTPServer, range_header: str
) -> None:
    reply = fetch(server, "/clip.webm", headers={"Range": range_header})

    assert reply.status == 200
    assert reply.body == DIGITS


def test_if_range_gets_the_whole_file(server: LoopbackHTTPServer) -> None:
    headers = {"Range": "bytes=0-1", "If-Range": '"an-etag"'}

    reply = fetch(server, "/clip.webm", headers=headers)

    assert reply.status == 200
    assert reply.body == DIGITS


def test_head_ignores_range(server: LoopbackHTTPServer) -> None:
    reply = fetch(server, "/clip.webm", method="HEAD", headers={"Range": "bytes=0-1"})

    assert reply.status == 200
    assert reply.headers["Content-Length"] == "10"


def test_an_empty_file_has_no_satisfiable_range(server: LoopbackHTTPServer) -> None:
    whole = fetch(server, "/empty.webm")
    ranged = fetch(server, "/empty.webm", headers={"Range": "bytes=0-"})

    assert (whole.status, whole.body, whole.headers["Content-Length"]) == (200, b"", "0")
    assert ranged.status == 416
    assert ranged.headers["Content-Range"] == "bytes */0"


def test_large_files_and_ranges_span_several_chunks(server: LoopbackHTTPServer) -> None:
    whole = fetch(server, "/big.bin")
    middle = fetch(server, "/big.bin", headers={"Range": "bytes=70000-200000"})

    assert whole.body == BIG
    assert middle.status == 206
    assert middle.body == BIG[70000:200001]


# Logging


def test_requests_are_logged_at_debug_level_not_printed(
    server: LoopbackHTTPServer,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")

    fetch(server, "/main.js")

    records = [record for record in caplog.records if record.name == "iterplay.serving"]
    assert any('"GET /main.js HTTP/1.1" 200' in record.getMessage() for record in records)
    assert {record.levelno for record in records} == {logging.DEBUG}
    assert "main.js" not in capfd.readouterr().err


def test_control_characters_are_escaped_in_the_log(
    server: LoopbackHTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")

    reply = send_raw(server, b"GET /\x1b[31mred HTTP/1.0\r\n\r\n")

    messages = [record.getMessage() for record in caplog.records]
    assert reply.startswith(b"HTTP/1.0 404 ")
    assert any("/\\x1b[31mred" in message for message in messages)
    assert not any("\x1b" in message for message in messages)


def test_handler_errors_are_logged_not_printed(
    server: LoopbackHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def broken(name: str) -> str:
        raise RuntimeError(f"cannot type {name}")

    monkeypatch.setattr("iterplay.serving._content_type", broken)

    with pytest.raises(http.client.RemoteDisconnected):
        fetch(server, "/main.js")

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors
    assert errors[0].exc_info is not None
    assert "cannot type main.js" in str(errors[0].exc_info[1])
    assert "Traceback" not in capfd.readouterr().err


# The game server


def test_game_server_serves_the_game_at_the_root(tmp_path: Path) -> None:
    game = write_game(tmp_path / "game")

    with game_server(game, port=0) as server:
        host, _ = server.server_address
        replies = {path: fetch(server, path) for path in ("/", "/main.js", "/level.json")}
        sprite = fetch(server, "/sprite.png")

    assert host == "127.0.0.1"
    assert replies["/"].body == (game / "index.html").read_bytes()
    assert replies["/main.js"].headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert replies["/level.json"].body == b'{"tiles": []}'
    assert sprite.headers["Content-Type"] == "image/png"


def test_a_game_without_vendor_gets_the_packaged_three(tmp_path: Path) -> None:
    game = write_game(tmp_path / "game")
    vendor = resource_dir(VENDOR_ASSETS)

    with game_server(game, port=0) as server:
        replies = {
            name: fetch(server, f"/vendor/{name}")
            for name in ("three.module.js", "three.core.js", "LICENSE", "README.md")
        }

    assert replies["three.module.js"].status == 200
    assert replies["three.module.js"].body == (vendor / "three.module.js").read_bytes()
    assert replies["three.module.js"].headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert replies["three.core.js"].body == (vendor / "three.core.js").read_bytes()
    assert replies["LICENSE"].status == 404
    assert replies["README.md"].status == 404


def test_a_game_with_vendor_serves_its_own_copy(tmp_path: Path) -> None:
    game = write_game(tmp_path / "game")
    write_tree(game / "vendor", {"three.module.js": b"// own copy\n", "LICENSE": b"MIT\n"})

    with game_server(game, port=0) as server:
        module = fetch(server, "/vendor/three.module.js")
        license_file = fetch(server, "/vendor/LICENSE")
        core = fetch(server, "/vendor/three.core.js")

    assert module.body == b"// own copy\n"
    assert license_file.status == 200
    assert core.status == 404


def test_the_reference_game_plays_without_copying_three() -> None:
    with game_server(REFERENCE_GAME, port=0) as server:
        page = fetch(server, "/")
        entry = fetch(server, "/main.js")
        three = fetch(server, "/vendor/three.module.js")

    assert page.status == 200
    assert b"./main.js" in page.body
    assert entry.status == 200
    assert three.status == 200


# The viewer server


def test_the_viewer_page_is_served_at_the_root(viewer: LoopbackHTTPServer) -> None:
    root = fetch(viewer, "/")
    named = fetch(viewer, "/viewer.html")

    assert viewer.server_address[0] == "127.0.0.1"
    assert root.status == 200
    assert root.body == VIEWER_PAGE.read_bytes()
    assert root.headers["Content-Type"] == "text/html; charset=utf-8"
    assert named.body == root.body


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/runs/index.json", "application/json"),
        ("/runs/r1/summary.json", "application/json"),
        ("/runs/r1/design_doc.md", "text/markdown; charset=utf-8"),
        ("/runs/r1/v1/score.json", "application/json"),
        ("/runs/r1/v1/critique.verified.json", "application/json"),
        ("/runs/r1/v1/changelog.md", "text/markdown; charset=utf-8"),
        ("/runs/r1/v1/playtest/random.webm", "video/webm"),
        ("/runs/r1/v1/playtest/combined_360p.mp4", "video/mp4"),
    ],
)
def test_the_viewer_serves_run_artifacts(
    viewer: LoopbackHTTPServer, path: str, content_type: str
) -> None:
    reply = fetch(viewer, path)

    assert reply.status == 200
    assert reply.body == RUN_FILES[path.removeprefix("/runs/")]
    assert reply.headers["Content-Type"] == content_type


@pytest.mark.parametrize(
    "path",
    [
        "/runs/r1/v1/game/main.js",
        "/runs/r1/v1/game/index.html",
        "/runs/r1/v1/game/",
        "/runs/r1/v1/game/vendor/three.module.js",
        "/runs/r1/v1/playtest/random.telemetry.jsonl",
        "/runs/r1/v1/playtest/console.log",
        "/runs/r1/.notes.json",
        "/runs/",
        "/runs",
        "/runs/r1/",
        "/runs/r1/v1",
        "/runs/../runs/index.json",
        "/runs/%2e%2e/runs/index.json",
        "/server.py",
        "/static/viewer.html",
        "/missing.html",
    ],
)
def test_the_viewer_serves_nothing_else(viewer: LoopbackHTTPServer, path: str) -> None:
    assert fetch(viewer, path).status == 404


def test_viewer_videos_can_seek(viewer: LoopbackHTTPServer) -> None:
    path = "/runs/r1/v1/playtest/random.webm"

    reply = fetch(viewer, path, headers={"Range": "bytes=2-5"})

    assert reply.status == 206
    assert reply.body == b"2345"


def test_the_viewer_page_mount_serves_only_html_js_and_css(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    names = ("viewer.html", "app.js", "style.css", "data.json", "notes.md", "server.py")
    write_tree(package / "static", {name: name.encode() for name in names})
    monkeypatch.setattr("iterplay.viewer.server.files", lambda _: package)

    with viewer_server(tmp_path / "runs", port=0) as server:
        statuses = {name: fetch(server, f"/{name}").status for name in names}
        root = fetch(server, "/")

    assert statuses == {
        "viewer.html": 200,
        "app.js": 200,
        "style.css": 200,
        "data.json": 404,
        "notes.md": 404,
        "server.py": 404,
    }
    assert root.body == b"viewer.html"


def test_artifact_suffixes_exclude_game_code() -> None:
    assert {".html", ".js", ".mjs", ".jsonl", ".log"}.isdisjoint(ARTIFACT_SUFFIXES)


# Real browsers


@pytest.mark.browser
def test_chromium_imports_the_packaged_three_as_a_module(tmp_path: Path) -> None:
    game = write_tree(
        tmp_path / "game",
        {
            "index.html": b'<!doctype html><link rel="icon" href="data:,">'
            b'<script type="module" src="./main.js"></script>',
            "main.js": b'import * as THREE from "./vendor/three.module.js";\n'
            b"window.vectorType = typeof THREE.Vector3;\n",
        },
    )
    errors: list[str] = []

    def record(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(message.text)

    with game_server(game, port=0) as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.on("console", record)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.base_url)
            page.wait_for_function("window.vectorType === 'function'", timeout=20_000)
        finally:
            browser.close()

    assert errors == []


@pytest.mark.browser
@pytest.mark.ffmpeg
def test_chromium_seeks_a_video_served_by_the_viewer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clip = tmp_path / "runs" / "r1" / "v1" / "playtest" / "random.webm"
    clip.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=4:size=64x64:rate=10",
            "-c:v",
            "libvpx",
            "-b:v",
            "200k",
            str(clip),
        ],
        check=True,
        timeout=60,
    )
    caplog.set_level(logging.DEBUG, logger="iterplay.serving")

    with viewer_server(tmp_path / "runs", port=0) as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(server.base_url)
            position = page.evaluate(SEEK_SCRIPT, ["runs/r1/v1/playtest/random.webm", 3])
        finally:
            browser.close()

    assert position == pytest.approx(3, abs=0.1)
    messages = [record.getMessage() for record in caplog.records]
    assert any('random.webm HTTP/1.1" 206' in message for message in messages)
