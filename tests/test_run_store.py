"""Runs are created, opened and indexed in the runs directory (HLD §6)."""

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from iterplay.errors import RunNotFoundError
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.run import RunConfig, RunIndex
from iterplay.storage import run_store
from iterplay.storage.artifacts import read_model, write_model
from iterplay.storage.layout import RunPaths
from iterplay.storage.run_store import RunStore

CREATED = datetime(2026, 10, 25, 10, 30, tzinfo=UTC)
DESIGN_DOC = "# Tunnel Turbulence\n\nRun, jump, collect every ★.\n"


def make_config(run_id: str, created_at: datetime = CREATED) -> RunConfig:
    item = RubricItem(
        id="boots_cleanly", description="Boots.", source=RubricSource.SMOKE, weight=1.0
    )
    return RunConfig(
        run_id=run_id,
        created_at=created_at,
        design_doc_sha256="0" * 64,
        rubric=Rubric(target=0.9, items=[item]),
        model="gemini-test",
        bot_seeds={bot: seed for seed, bot in enumerate(BotName, start=1)},
    )


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "runs"


@pytest.fixture
def store(runs_dir: Path) -> RunStore:
    return RunStore(runs_dir)


def test_create_writes_config_and_design_doc(store: RunStore, runs_dir: Path) -> None:
    config = make_config("tunnel-1")

    run = store.create(config, DESIGN_DOC)

    assert run == RunPaths(runs_dir / "tunnel-1")
    assert run.run_id == "tunnel-1"
    assert read_model(run.config, RunConfig) == config
    assert run.design_doc.read_text(encoding="utf-8") == DESIGN_DOC


def test_create_refuses_an_existing_run(store: RunStore) -> None:
    run = store.create(make_config("tunnel-1"), DESIGN_DOC)

    with pytest.raises(FileExistsError):
        store.create(make_config("tunnel-1", CREATED + timedelta(days=1)), "# Other doc\n")

    assert read_model(run.config, RunConfig) == make_config("tunnel-1")
    assert run.design_doc.read_text(encoding="utf-8") == DESIGN_DOC


def test_create_refuses_a_leftover_run_directory(store: RunStore, runs_dir: Path) -> None:
    (runs_dir / "tunnel-1").mkdir(parents=True)

    with pytest.raises(FileExistsError):
        store.create(make_config("tunnel-1"), DESIGN_DOC)

    assert list((runs_dir / "tunnel-1").iterdir()) == []


def test_a_failed_create_leaves_no_run_behind(
    store: RunStore, runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(run_store, "write_text", fail)

    with pytest.raises(OSError, match="disk full"):
        store.create(make_config("tunnel-1"), DESIGN_DOC)

    assert not (runs_dir / "tunnel-1").exists()
    monkeypatch.undo()
    assert store.create(make_config("tunnel-1"), DESIGN_DOC).config.is_file()


def test_open_returns_the_layout_of_an_existing_run(store: RunStore) -> None:
    run = store.create(make_config("tunnel-1"), DESIGN_DOC)

    assert store.open(run.root) == run


def test_open_works_on_runs_outside_the_runs_directory(tmp_path: Path, store: RunStore) -> None:
    elsewhere = RunStore(tmp_path / "elsewhere").create(make_config("other"), DESIGN_DOC)

    assert store.open(elsewhere.root) == elsewhere


def test_open_requires_a_config(store: RunStore, runs_dir: Path) -> None:
    (runs_dir / "empty").mkdir(parents=True)
    (runs_dir / "odd" / "config.json").mkdir(parents=True)

    for run_dir in (runs_dir / "missing", runs_dir / "empty", runs_dir / "odd"):
        with pytest.raises(RunNotFoundError, match=r"config\.json"):
            store.open(run_dir)


def test_no_runs_are_listed_before_the_runs_directory_exists(store: RunStore) -> None:
    assert store.list_run_ids() == []


def test_runs_are_listed_oldest_first_with_ties_broken_by_id(store: RunStore) -> None:
    store.create(make_config("c", CREATED), DESIGN_DOC)
    store.create(make_config("a", CREATED + timedelta(hours=1)), DESIGN_DOC)
    store.create(make_config("b", CREATED), DESIGN_DOC)
    store.create(make_config("z", CREATED - timedelta(days=1)), DESIGN_DOC)

    assert store.list_run_ids() == ["z", "b", "c", "a"]


def test_naive_and_aware_creation_times_are_compared_as_utc(store: RunStore) -> None:
    store.create(make_config("naive", datetime(2026, 10, 25, 9, 0)), DESIGN_DOC)
    store.create(make_config("utc", datetime(2026, 10, 25, 10, 0, tzinfo=UTC)), DESIGN_DOC)
    plus_two = timezone(timedelta(hours=2))
    store.create(make_config("offset", datetime(2026, 10, 25, 10, 30, tzinfo=plus_two)), DESIGN_DOC)

    assert store.list_run_ids() == ["offset", "naive", "utc"]


def test_only_directories_with_a_readable_config_are_listed(
    store: RunStore, runs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store.create(make_config("good"), DESIGN_DOC)
    (runs_dir / "no-config").mkdir()
    (runs_dir / "broken").mkdir()
    (runs_dir / "broken" / "config.json").write_text("{not json", encoding="utf-8")
    (runs_dir / "wrong").mkdir()
    (runs_dir / "wrong" / "config.json").write_text('{"run_id": "wrong"}', encoding="utf-8")
    write_model(runs_dir / ".hidden" / "config.json", make_config("hidden"))
    (runs_dir / "notes.txt").write_text("not a run", encoding="utf-8")
    store.write_index()

    with caplog.at_level(logging.WARNING, logger="iterplay.storage.run_store"):
        assert store.list_run_ids() == ["good"]

    warned = " ".join(record.getMessage() for record in caplog.records)
    assert "broken" in warned
    assert "wrong" in warned
    assert "no-config" not in warned


def test_runs_are_listed_by_directory_name(store: RunStore, runs_dir: Path) -> None:
    write_model(runs_dir / "renamed" / "config.json", make_config("original"))

    assert store.list_run_ids() == ["renamed"]


def test_write_index_creates_the_runs_directory(store: RunStore, runs_dir: Path) -> None:
    path = store.write_index()

    assert path == runs_dir / "index.json"
    assert read_model(path, RunIndex) == RunIndex(run_ids=[])


def test_write_index_lists_every_run_oldest_first(store: RunStore) -> None:
    store.create(make_config("second", CREATED + timedelta(minutes=5)), DESIGN_DOC)
    store.create(make_config("first", CREATED), DESIGN_DOC)

    path = store.write_index()

    assert json.loads(path.read_text(encoding="utf-8")) == {"run_ids": ["first", "second"]}
