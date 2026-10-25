"""Game directories are written and read back without escaping their sandbox (HLD §3.1, §9)."""

from pathlib import Path

import pytest

from iterplay.contract.layout import VENDOR_DIR
from iterplay.errors import GuardrailError, UnsafePathError
from iterplay.models.game import GameBundle, GameFile
from iterplay.storage import game_files
from iterplay.storage.game_files import VENDOR_ASSETS, read_game, resolve_within, write_game

INDEX = '<!doctype html>\n<script type="module" src="./main.js"></script>\n'
MAIN = 'import * as THREE from "./vendor/three.module.js";\r\nconsole.log("héllo ★");\n'


def make_bundle(files: dict[str, str]) -> GameBundle:
    return GameBundle(
        files=[GameFile(path=path, content=content) for path, content in files.items()]
    )


@pytest.fixture
def game() -> GameBundle:
    return make_bundle({"main.js": MAIN, "index.html": INDEX, "player.js": "export const v = 7;\n"})


@pytest.fixture
def game_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "abc" / "v1" / "game"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory beside the game that nothing may touch, holding ``secret.txt``."""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_text("keep me", encoding="utf-8")
    return directory


def names(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


@pytest.mark.parametrize("relative", ["main.js", "./main.js", "levels/one.js", "a//b.js", "a.b/c"])
def test_resolve_within_accepts_paths_inside_root(tmp_path: Path, relative: str) -> None:
    resolved = resolve_within(tmp_path, relative)

    assert resolved == (tmp_path / relative).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "relative",
    [
        "",
        ".",
        "./",
        "/etc/passwd",
        "//server/share/main.js",
        "C:\\game\\main.js",
        "C:/game/main.js",
        "C:main.js",
        "levels\\one.js",
        "..",
        "../main.js",
        "levels/../../main.js",
        "levels/..",
        "levels/../main.js",
        "main.js\x00.html",
    ],
)
def test_resolve_within_refuses_unsafe_paths(tmp_path: Path, relative: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path / "root", relative)


def test_resolve_within_refuses_symlink_escapes(tmp_path: Path, outside: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "secret.js").symlink_to(outside / "secret.txt")

    with pytest.raises(UnsafePathError, match="outside"):
        resolve_within(root, "linked/main.js")
    with pytest.raises(UnsafePathError, match="outside"):
        resolve_within(root, "secret.js")


def test_resolve_within_follows_symlinks_that_stay_inside(tmp_path: Path) -> None:
    (tmp_path / "main.js").write_text("", encoding="utf-8")
    (tmp_path / "alias.js").symlink_to(tmp_path / "main.js")

    assert resolve_within(tmp_path, "alias.js") == (tmp_path / "main.js").resolve()


def test_resolve_within_accepts_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    assert resolve_within(tmp_path / "link", "main.js") == real.resolve() / "main.js"


def test_write_game_writes_the_bundle_and_vendored_threejs(
    game_dir: Path, game: GameBundle
) -> None:
    write_game(game_dir, game)

    assert names(game_dir) == {"index.html", "main.js", "player.js", VENDOR_DIR}
    for file in game.files:
        assert (game_dir / file.path).read_bytes() == file.content.encode("utf-8")
    vendor = game_dir / VENDOR_DIR
    vendored_js = {entry.name for entry in VENDOR_ASSETS.iterdir() if entry.name.endswith(".js")}
    assert names(vendor) == vendored_js | {"LICENSE"}
    assert {"three.module.js", "three.core.js", "LICENSE"} <= names(vendor)
    assert "README.md" not in names(vendor)
    for name in names(vendor):
        assert (vendor / name).read_bytes() == (VENDOR_ASSETS / name).read_bytes()


def test_write_game_replaces_existing_contents(game_dir: Path, game: GameBundle) -> None:
    (game_dir / "levels" / "deep").mkdir(parents=True)
    (game_dir / "levels" / "deep" / "old.js").write_text("old", encoding="utf-8")
    (game_dir / "old.js").write_text("old", encoding="utf-8")
    (game_dir / ".DS_Store").write_bytes(b"\x00\x01")
    (game_dir / VENDOR_DIR).mkdir()
    (game_dir / VENDOR_DIR / "stale.js").write_text("old", encoding="utf-8")

    write_game(game_dir, game)

    assert names(game_dir) == {"index.html", "main.js", "player.js", VENDOR_DIR}
    assert "stale.js" not in names(game_dir / VENDOR_DIR)


def test_write_game_removes_symlinks_without_following_them(
    game_dir: Path, game: GameBundle, outside: Path
) -> None:
    game_dir.mkdir(parents=True)
    (game_dir / "linked").symlink_to(outside, target_is_directory=True)
    (game_dir / "main.js").symlink_to(outside / "secret.txt")

    write_game(game_dir, game)

    assert names(outside) == {"secret.txt"}
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep me"
    assert not (game_dir / "main.js").is_symlink()
    assert (game_dir / "main.js").read_bytes() == MAIN.encode("utf-8")


def test_write_game_refuses_unsafe_paths_before_deleting_anything(
    tmp_path: Path, game_dir: Path
) -> None:
    game_dir.mkdir(parents=True)
    (game_dir / "old.js").write_text("old", encoding="utf-8")
    bundle = make_bundle({"index.html": INDEX, "../escape.js": "evil"})

    with pytest.raises(UnsafePathError):
        write_game(game_dir, bundle)

    assert names(game_dir) == {"old.js"}
    assert not (game_dir.parent / "escape.js").exists()


@pytest.mark.parametrize("path", ["vendor/three.module.js", "./vendor/evil.js", "Vendor/x.js"])
def test_write_game_refuses_the_reserved_vendor_directory(game_dir: Path, path: str) -> None:
    with pytest.raises(GuardrailError, match="reserved"):
        write_game(game_dir, make_bundle({"index.html": INDEX, path: "evil"}))

    assert not game_dir.exists()


def test_write_game_refuses_a_symlinked_game_directory(
    tmp_path: Path, game: GameBundle, outside: Path
) -> None:
    game_dir = tmp_path / "game"
    game_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError, match="symlink"):
        write_game(game_dir, game)

    assert names(outside) == {"secret.txt"}


def test_write_game_refuses_to_run_without_vendored_threejs(
    tmp_path: Path, game_dir: Path, game: GameBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete = tmp_path / "vendor-assets"
    incomplete.mkdir()
    (incomplete / "README.md").write_text("Vendor Three.js here.", encoding="utf-8")
    (incomplete / "three.core.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setattr(game_files, "VENDOR_ASSETS", incomplete)
    game_dir.mkdir(parents=True)
    (game_dir / "old.js").write_text("old", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match=r"LICENSE, three\.module\.js"):
        write_game(game_dir, game)

    assert names(game_dir) == {"old.js"}


def test_write_game_creates_subdirectories_for_nested_paths(game_dir: Path) -> None:
    write_game(game_dir, make_bundle({"index.html": INDEX, "levels/one.js": "export {};\n"}))

    assert (game_dir / "levels" / "one.js").read_text(encoding="utf-8") == "export {};\n"


def test_read_game_round_trips_write_game(game_dir: Path, game: GameBundle) -> None:
    write_game(game_dir, game)

    loaded = read_game(game_dir)

    assert loaded == GameBundle(files=sorted(game.files, key=lambda file: file.path))
    assert next(f for f in loaded.files if f.path == "main.js").content == MAIN


def test_read_game_skips_vendor_and_sorts_posix_paths(game_dir: Path) -> None:
    write_game(
        game_dir,
        make_bundle({"main.js": "", "index.html": "", "b/z.js": "", "a.js": "", "b/a.js": ""}),
    )

    assert [file.path for file in read_game(game_dir).files] == [
        "a.js",
        "b/a.js",
        "b/z.js",
        "index.html",
        "main.js",
    ]


def test_read_game_skips_symlinks_and_hidden_files(
    game_dir: Path, game: GameBundle, outside: Path
) -> None:
    write_game(game_dir, game)
    (game_dir / ".DS_Store").write_bytes(b"\x00\x01\xff")
    (game_dir / "._main.js").write_bytes(b"\x00\x05\x16\x07\xff")
    (game_dir / ".cache").mkdir()
    (game_dir / ".cache" / "x.js").write_text("hidden", encoding="utf-8")
    (game_dir / "secret.js").symlink_to(outside / "secret.txt")
    (game_dir / "linked").symlink_to(outside, target_is_directory=True)

    assert [file.path for file in read_game(game_dir).files] == [
        "index.html",
        "main.js",
        "player.js",
    ]


def test_read_game_refuses_files_that_are_not_utf8(game_dir: Path, game: GameBundle) -> None:
    write_game(game_dir, game)
    (game_dir / "level.js").write_bytes(b"const x = '\xff';\n")

    with pytest.raises(GuardrailError, match=r"level\.js"):
        read_game(game_dir)


def test_read_game_of_an_empty_directory_is_an_empty_bundle(game_dir: Path) -> None:
    game_dir.mkdir(parents=True)

    assert read_game(game_dir) == GameBundle(files=[])


def test_read_game_raises_file_not_found_for_a_missing_directory(game_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_game(game_dir)
