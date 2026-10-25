"""The use case behind each CLI command (HLD §7), and the composition root that wires them.

``new_run`` is driven end to end with the scripted model double, so no test needs an API key
or the network; its servers bind 127.0.0.1 on ports the OS picks. Input checks run before
any browser is launched, which the tests enforce by making a launch fail.
"""

import builtins
import hashlib
import json
import math
import re
import shutil
import socket
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from iterplay import app
from iterplay.agents.critic.direct import DirectCalls
from iterplay.agents.critic.managed import NOT_IMPLEMENTED
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.contract.templates import DEFAULT_TEMPLATE, TEMPLATES, TemplateId
from iterplay.errors import ConfigError, RunNotFoundError
from iterplay.evaluation.rubric import load_rubric
from iterplay.evaluation.scorer import DEFAULT_MIN_FPS
from iterplay.harness.playtest import PlaytestConfig
from iterplay.llm.metering import UsageMeter
from iterplay.models.critique import Critique, EvidenceType, Issue, Severity
from iterplay.models.game import BuilderResponse, ChangelogEntry, FixerResponse, GameFile
from iterplay.models.playtest import BotName, PlaytestReport
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.run import (
    Budgets,
    CriticBackendKind,
    RunConfig,
    RunIndex,
    RunSummary,
    StopReason,
    TokenUsage,
    VersionOutcome,
    VersionRecord,
)
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)
from iterplay.settings import Settings
from iterplay.storage.artifacts import read_model, write_model, write_text
from iterplay.storage.layout import RunPaths, VersionPaths
from iterplay.storage.run_store import RunStore
from tests.helpers import FakeLLMClient

REPO = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "games"
REFERENCE_GAME = REPO / "examples" / "reference_game"
RUBRIC_FILE = REPO / "rubric.yaml"
DESIGN_DOC = "# Gem dash\n\nRun right to the flag, collecting both gems on the way.\n"
URL = re.compile(r"http://127\.0\.0\.1:\d+/\S*")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that never read ``.env``, with the servers on ports the OS picks."""
    defaults = Settings(runs_dir=tmp_path / "runs")
    return defaults.model_copy(update={"game_port": 0, "viewer_port": 0})


@pytest.fixture
def design_doc(tmp_path: Path) -> Path:
    path = tmp_path / "docs" / "Gem Dash!.md"
    path.parent.mkdir()
    path.write_text(DESIGN_DOC, encoding="utf-8")
    return path


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if ``new_run`` gets as far as launching a browser."""

    def launch(*args: object, **kwargs: object) -> None:
        raise AssertionError("new_run launched a browser before its inputs were checked")

    monkeypatch.setattr(app, "HarnessBrowser", launch)


def options(design_doc: Path, **overrides: Any) -> app.NewRunOptions:
    values: dict[str, Any] = {
        "design_doc": design_doc,
        "rubric": RUBRIC_FILE,
        "max_iterations": 1,
        "critic_backend": CriticBackendKind.DIRECT,
        "headless": True,
        "template": TemplateId.PLATFORMER_2D,
        "run_id": "test-run",
    }
    values.update(overrides)
    return app.NewRunOptions(**values)


def fixture_game(name: str) -> list[GameFile]:
    return [
        GameFile(path=path.name, content=path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURES / name).iterdir())
    ]


def critique(rating: int = 3) -> Critique:
    issue = Issue(
        id="I1",
        bot=BotName.SPEEDRUNNER,
        timestamp="00:01",
        rubric_item="game_feel",
        severity=Severity.LOW,
        observation="Jumps feel floaty.",
        evidence_type=EvidenceType.VISUAL,
    )
    return Critique(
        issues=[issue],
        rubric_assessment={"matches_design_doc": rating, "game_feel": rating},
    )


def cached_purposes(version: VersionPaths) -> list[str]:
    return sorted(
        json.loads(entry.read_text(encoding="utf-8"))["request"]["purpose"]
        for entry in version.llm_dir.glob("*.json")
    )


def fetch(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:
        status: int = response.status
        body: bytes = response.read()
    return status, body


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A port something else is already listening on, on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener.getsockname()[1]


# iterplay new


@pytest.mark.browser
@pytest.mark.ffmpeg
async def test_new_run_builds_scores_and_fixes_a_game_end_to_end(
    settings: Settings, design_doc: Path
) -> None:
    simple = fixture_game("simple")
    entry = ChangelogEntry(issue_ids=["I1"], summary="Snappier jumps")
    llm = FakeLLMClient(
        {
            "builder": [BuilderResponse(files=simple)],
            "critic": [critique(1)] * 2,
            "fixer": [FixerResponse(files=simple, changelog=[entry])],
        }
    )

    summary = await app.new_run(
        options(design_doc, max_iterations=2, run_id=None),
        settings,
        llm=llm,
        playtest=PlaytestConfig(max_duration_s=3.0),
    )

    assert re.fullmatch(r"\d{8}-\d{6}-gem-dash", summary.run_id)
    index = read_model(settings.runs_dir / "index.json", RunIndex)
    assert index.run_ids == [summary.run_id]
    run = RunPaths(settings.runs_dir / summary.run_id)
    assert read_model(run.summary, RunSummary) == summary
    assert run.design_doc.read_bytes() == DESIGN_DOC.encode()
    config = read_model(run.config, RunConfig)
    assert config.run_id == summary.run_id
    assert config.design_doc_sha256 == hashlib.sha256(DESIGN_DOC.encode()).hexdigest()
    assert config.template is TemplateId.PLATFORMER_2D
    assert config.rubric == load_rubric(RUBRIC_FILE)
    assert config.model == settings.model
    assert config.bot_seeds == {BotName.RANDOM: 1, BotName.SPEEDRUNNER: 2, BotName.NEWBIE: 3}
    assert config.budgets == Budgets(max_iterations=2)
    assert (config.critic_backend, config.headless) == (CriticBackendKind.DIRECT, True)

    assert summary.stop_reason is StopReason.MAX_ITERATIONS
    assert [record.version for record in summary.versions] == [1, 2]
    assert summary.versions[1].parent_version == 1
    assert summary.token_usage.input_tokens > 0
    for record in summary.versions:
        version = run.version(record.version)
        assert read_model(version.smoke_report, SmokeReport).passed
        report = read_model(version.playtest_report, PlaytestReport)
        assert [result.bot for result in report.runs] == list(BotName)
        assert all(result.duration_s <= 3.0 for result in report.runs)
        assert version.combined_video.stat().st_size > 1000
        assert read_model(version.score, ScoreCard).total == record.score
    assert [request.purpose for request in llm.requests] == ["builder", "critic", "fixer", "critic"]
    first_critic = llm.requests_for("critic")[0]
    assert [media.path for media in first_critic.media] == [run.version(1).combined_video]
    assert cached_purposes(run.version(1)) == ["builder", "critic"]
    assert cached_purposes(run.version(2)) == ["critic", "fixer"]
    assert run.version(2).changelog.read_text(encoding="utf-8") == "- [I1] Snappier jumps\n"


@pytest.mark.parametrize(
    ("contents", "message"),
    [(None, "Cannot read design doc"), (b" \n\t\n", "is empty"), (b"\xff\xfe", "not UTF-8")],
    ids=["missing", "blank", "not utf-8"],
)
@pytest.mark.usefixtures("no_browser")
async def test_new_run_refuses_an_unusable_design_doc(
    tmp_path: Path, settings: Settings, contents: bytes | None, message: str
) -> None:
    doc = tmp_path / "doc.md"
    if contents is not None:
        doc.write_bytes(contents)

    with pytest.raises(ConfigError, match=message):
        await app.new_run(options(doc), settings, llm=FakeLLMClient())

    assert not settings.runs_dir.exists()


@pytest.mark.usefixtures("no_browser")
async def test_new_run_refuses_an_invalid_rubric(
    tmp_path: Path, settings: Settings, design_doc: Path
) -> None:
    rubric = tmp_path / "rubric.yaml"
    rubric.write_text("target: 2\nitems: []\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"rubric\.yaml is invalid"):
        await app.new_run(options(design_doc, rubric=rubric), settings, llm=FakeLLMClient())

    assert not settings.runs_dir.exists()


@pytest.mark.usefixtures("no_browser")
async def test_new_run_needs_an_api_key_unless_given_a_model_client(
    settings: Settings, design_doc: Path
) -> None:
    assert settings.gemini_api_key is None

    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        await app.new_run(options(design_doc), settings)

    assert not settings.runs_dir.exists()


@pytest.mark.usefixtures("no_browser")
async def test_the_managed_critic_fails_before_the_run_exists(
    settings: Settings, design_doc: Path
) -> None:
    run = options(design_doc, critic_backend=CriticBackendKind.MANAGED)

    with pytest.raises(ConfigError) as raised:
        await app.new_run(run, settings, llm=FakeLLMClient())

    assert str(raised.value) == NOT_IMPLEMENTED
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"run_id": "../escape"}, r"run_id: String should match pattern .* \(got '../escape'\)"),
        ({"run_id": ".hidden"}, "run_id"),
        ({"max_iterations": 0}, "max_iterations: Input should be greater than or equal to 1"),
    ],
    ids=["path in id", "hidden id", "no iterations"],
)
@pytest.mark.usefixtures("no_browser")
async def test_new_run_refuses_invalid_run_settings(
    settings: Settings, design_doc: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        await app.new_run(options(design_doc, **overrides), settings, llm=FakeLLMClient())

    assert not settings.runs_dir.exists()
    assert not (settings.runs_dir.parent / "escape").exists()


@pytest.mark.usefixtures("no_browser")
async def test_new_run_needs_ffmpeg(
    settings: Settings, design_doc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(ConfigError, match="ffmpeg and ffprobe not found on PATH"):
        await app.new_run(options(design_doc), settings, llm=FakeLLMClient())

    assert not settings.runs_dir.exists()


@pytest.mark.usefixtures("no_browser")
async def test_new_run_refuses_a_busy_game_port(
    settings: Settings, design_doc: Path, busy_port: int
) -> None:
    busy = settings.model_copy(update={"game_port": busy_port})

    with pytest.raises(ConfigError, match=f"127.0.0.1:{busy_port}.*ITERPLAY_GAME_PORT"):
        await app.new_run(options(design_doc), busy, llm=FakeLLMClient())

    assert not settings.runs_dir.exists()


def test_a_run_id_cannot_be_reused(tmp_path: Path, settings: Settings, design_doc: Path) -> None:
    config = app._run_config(options(design_doc), settings, DESIGN_DOC, load_rubric(RUBRIC_FILE))
    store = RunStore(settings.runs_dir)
    store.create(config, DESIGN_DOC)

    with pytest.raises(ConfigError, match="'test-run' already exists"):
        app._create_run(store, config, DESIGN_DOC, settings.runs_dir)


def test_the_run_config_records_how_to_reproduce_the_run(
    settings: Settings, design_doc: Path
) -> None:
    rubric = load_rubric(RUBRIC_FILE)
    before = datetime.now(UTC)

    config = app._run_config(options(design_doc, max_iterations=4), settings, DESIGN_DOC, rubric)

    assert config.run_id == "test-run"
    assert before <= config.created_at <= datetime.now(UTC)
    assert config.design_doc_sha256 == hashlib.sha256(DESIGN_DOC.encode()).hexdigest()
    assert (config.rubric, config.model, config.game_seed) == (rubric, settings.model, 1)
    assert config.bot_seeds == dict(app.BOT_SEEDS)
    assert config.budgets == Budgets(max_iterations=4)
    assert config.pricing is None


@pytest.mark.parametrize("run_id", [None, ""])
def test_a_missing_run_id_is_generated_from_the_time_and_the_doc(
    settings: Settings, design_doc: Path, run_id: str | None
) -> None:
    rubric = load_rubric(RUBRIC_FILE)

    config = app._run_config(options(design_doc, run_id=run_id), settings, DESIGN_DOC, rubric)

    stamp = config.created_at.strftime("%Y%m%d-%H%M%S")
    assert config.run_id == f"{stamp}-gem-dash"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tunnel_turbulence.md", "20261025-143000-tunnel-turbulence"),
        ("Über Spiel (v2).md", "20261025-143000-uber-spiel-v2"),
        ("--.md", "20261025-143000"),
        ("游戏.md", "20261025-143000"),
        (f"{'a' * 39}-tail.md", f"20261025-143000-{'a' * 39}"),
    ],
)
def test_generated_run_ids_are_utc_stamps_and_slugs(name: str, expected: str) -> None:
    pacific = timezone(timedelta(hours=-7))
    created_at = datetime(2026, 10, 25, 7, 30, tzinfo=pacific)

    assert app._generated_run_id(Path("docs") / name, created_at) == expected


# Wiring.


async def test_a_versions_agents_meter_their_calls_and_cache_them_in_its_llm_dir(
    tmp_path: Path,
) -> None:
    replies = [BuilderResponse(files=fixture_game("simple"))] * 2
    llm = FakeLLMClient({"builder": replies})
    meter = UsageMeter(Budgets(), None)
    prompts = PromptLibrary(TEMPLATES[DEFAULT_TEMPLATE])
    made: list[DirectCalls] = []

    def critic(client: Any) -> DirectCalls:
        made.append(DirectCalls(client, prompts))
        return made[-1]

    factory = app._agent_factory(llm, meter, prompts, critic, model_name="test-model")
    v1, v2 = VersionPaths(tmp_path / "v1"), VersionPaths(tmp_path / "v2")

    agents = factory(v1)
    first = await agents.builder.build(DESIGN_DOC)
    used = meter.total
    again = await factory(v1).builder.build(DESIGN_DOC)
    await factory(v2).builder.build(DESIGN_DOC)

    assert agents.critic is made[0]
    assert again == first
    assert used.input_tokens > 0
    assert len(llm.requests) == 2
    assert meter.total == TokenUsage(
        input_tokens=2 * used.input_tokens, output_tokens=2 * used.output_tokens
    )
    assert cached_purposes(v1) == ["builder"]
    assert cached_purposes(v2) == ["builder"]


def test_the_direct_critic_is_made_per_client() -> None:
    prompts = PromptLibrary(TEMPLATES[DEFAULT_TEMPLATE])
    factory = app._critic_factory(CriticBackendKind.DIRECT, Settings(), prompts)

    first, second = factory(FakeLLMClient()), factory(FakeLLMClient())

    assert isinstance(first, DirectCalls)
    assert first is not second


def test_the_managed_critic_is_not_available() -> None:
    prompts = PromptLibrary(TEMPLATES[DEFAULT_TEMPLATE])

    with pytest.raises(ConfigError, match="--critic direct"):
        app._critic_factory(CriticBackendKind.MANAGED, Settings(), prompts)


def test_the_verifier_uses_the_rubrics_fps_threshold() -> None:
    performance = RubricItem(
        id="performance",
        description="Fast enough.",
        source=RubricSource.TELEMETRY,
        weight=1,
        params={"min_fps": 45},
    )
    feel = RubricItem(id="feel", description="Fun.", source=RubricSource.CRITIC, weight=1)

    assert app._min_fps(load_rubric(RUBRIC_FILE)) == 30
    assert app._min_fps(Rubric(target=0.9, items=[performance, feel])) == 45
    assert app._min_fps(Rubric(target=0.9, items=[feel])) == DEFAULT_MIN_FPS


# iterplay play


def test_a_version_directory_is_played_from_its_game_directory(tmp_path: Path) -> None:
    game = tmp_path / "v3" / "game"
    game.mkdir(parents=True)
    (game / "index.html").write_text("<!doctype html>", encoding="utf-8")

    assert app._game_dir(tmp_path / "v3") == game
    assert app._game_dir(game) == game
    assert app._game_dir(REFERENCE_GAME) == REFERENCE_GAME


@pytest.mark.parametrize("layout", ["empty", "missing", "game without index"])
def test_a_directory_without_a_game_cannot_be_played(tmp_path: Path, layout: str) -> None:
    version = tmp_path / "v1"
    if layout != "missing":
        version.mkdir()
    if layout == "game without index":
        (version / "game").mkdir()

    with pytest.raises(ConfigError, match="No game in"):
        app._game_dir(version)


def test_a_game_is_played_in_real_time_without_the_harness_hud() -> None:
    assert app._play_url("http://127.0.0.1:8737/", 7) == "http://127.0.0.1:8737/index.html?seed=7"


def test_play_serves_the_game_and_opens_it_in_a_browser(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    served: dict[str, tuple[int, bytes]] = {}

    def serve_until_interrupted() -> None:
        (url,) = opened
        base = url.split("index.html")[0]
        served["page"] = fetch(url)
        served["three"] = fetch(f"{base}vendor/three.module.js")

    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(app, "_wait_for_interrupt", serve_until_interrupted)

    app.play(REFERENCE_GAME, settings, seed=4)

    (url,) = opened
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/index\.html\?seed=4", url)
    assert served["page"] == (200, (REFERENCE_GAME / "index.html").read_bytes())
    assert served["three"][0] == 200
    assert url in capsys.readouterr().out
    with pytest.raises(urllib.error.URLError):
        fetch(url)


def test_play_refuses_a_busy_port_before_opening_a_browser(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, busy_port: int
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    busy = settings.model_copy(update={"game_port": busy_port})

    with pytest.raises(ConfigError, match="ITERPLAY_GAME_PORT"):
        app.play(REFERENCE_GAME, busy)

    assert opened == []


def test_waiting_for_ctrl_c_returns_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupted)

    app._wait_for_interrupt()


# iterplay view


def test_the_runs_index_lists_every_run_oldest_first(tmp_path: Path, settings: Settings) -> None:
    store = RunStore(settings.runs_dir)
    for run_id, hour in (("late", 12), ("early", 9)):
        created_at = datetime(2026, 10, 25, hour, tzinfo=UTC)
        store.create(run_config(run_id, created_at=created_at), DESIGN_DOC)

    index = app._write_index(settings.runs_dir)

    assert index == RunIndex(run_ids=["early", "late"])
    assert read_model(settings.runs_dir / "index.json", RunIndex) == index


def test_the_runs_index_is_written_even_before_any_run(settings: Settings) -> None:
    assert app._write_index(settings.runs_dir) == RunIndex(run_ids=[])
    assert (settings.runs_dir / "index.json").is_file()


def test_view_refreshes_the_index_and_serves_the_viewer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    RunStore(settings.runs_dir).create(run_config("demo"), DESIGN_DOC)
    served: dict[str, tuple[int, bytes]] = {}

    def serve_until_interrupted() -> None:
        match = URL.search(capsys.readouterr().out)
        assert match is not None
        served["page"] = fetch(match.group(0))
        served["index"] = fetch(f"{match.group(0)}runs/index.json")

    monkeypatch.setattr(app, "_wait_for_interrupt", serve_until_interrupted)

    app.view(settings)

    status, page = served["page"]
    assert status == 200
    assert b"<html" in page.lower()
    assert served["index"] == (200, (settings.runs_dir / "index.json").read_bytes())
    assert RunIndex.model_validate_json(served["index"][1]).run_ids == ["demo"]


def test_view_refuses_a_busy_port(settings: Settings, busy_port: int) -> None:
    busy = settings.model_copy(update={"viewer_port": busy_port})

    with pytest.raises(ConfigError, match="ITERPLAY_VIEWER_PORT"):
        app.view(busy)


# iterplay replay


def run_config(run_id: str, *, created_at: datetime | None = None) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        created_at=created_at or datetime.now(UTC),
        design_doc_sha256="0" * 64,
        rubric=load_rubric(RUBRIC_FILE),
        model="test-model",
        bot_seeds=dict(app.BOT_SEEDS),
    )


def verified_issue(
    issue_id: str,
    status: VerificationStatus,
    *,
    origin: IssueOrigin = IssueOrigin.CRITIC,
    **fields: Any,
) -> VerifiedIssue:
    """A verified high-severity collision claim, unless ``fields`` say otherwise."""
    defaults = {"rubric_item": "collisions", "severity": "high", "evidence_type": "visual"}
    issue = Issue.model_validate({"id": issue_id, **defaults, **fields})
    return VerifiedIssue(issue=issue, origin=origin, status=status, rationale="Checked.")


def score(version: int, total: float) -> ScoreCard:
    return ScoreCard(version=version, items=[], total=total, target=0.9, meets_target=False)


def record(
    version: int, parent: int | None, total: float, outcome: VersionOutcome
) -> VersionRecord:
    return VersionRecord(
        version=version,
        parent_version=parent,
        score=total,
        outcome=outcome,
        duration_s=60,
        token_usage=TokenUsage(input_tokens=1000, output_tokens=100),
    )


@pytest.fixture
def finished_run(settings: Settings) -> RunPaths:
    """Three versions: one accepted with issues and a recording, one that never booted, and
    one accepted with patch notes."""
    run = RunStore(settings.runs_dir).create(run_config("demo"), DESIGN_DOC)
    v1, v2, v3 = run.version(1), run.version(2), run.version(3)
    write_model(v1.score, score(1, 0.55))
    issues = [
        verified_issue(
            "I1",
            VerificationStatus.CONFIRMED,
            observation="Player passes through\nthe second wall.",
            bot=BotName.SPEEDRUNNER,
            timestamp="00:42",
        ),
        verified_issue(
            "I2",
            VerificationStatus.CONTRADICTED,
            observation="Frame rate drops in the cave.",
            rubric_item="performance",
            severity=Severity.MEDIUM,
        ),
        verified_issue(
            "D1",
            VerificationStatus.CONFIRMED,
            observation="TypeError: boom " * 30,
            origin=IssueOrigin.DETERMINISTIC,
            rubric_item="no_runtime_errors",
        ),
    ]
    write_model(v1.critique_verified, VerifiedCritique(issues=issues))
    write_text(v1.changelog, "")
    v1.playtest_dir.mkdir(parents=True)
    v1.raw_video(BotName.SPEEDRUNNER).write_bytes(b"webm")
    write_model(v2.score, score(2, 0.0))
    write_text(v2.changelog, "- [D1] Rewrite the physics\n")
    write_model(v3.score, score(3, 0.725))
    write_model(v3.critique_verified, VerifiedCritique(issues=[]))
    write_text(v3.changelog, "- [I1, D1] Register the wall colliders\n\n- [I3] Snappier jumps\n")
    versions = [
        record(1, None, 0.55, VersionOutcome.ACCEPTED),
        record(2, 1, 0.0, VersionOutcome.BUILD_FAILED),
        record(3, 1, 0.725, VersionOutcome.ACCEPTED),
    ]
    summary = RunSummary(
        run_id="demo", versions=versions, best_version=3, stop_reason=StopReason.MAX_ITERATIONS
    )
    write_model(run.summary, summary)
    return run


def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record ``time.sleep`` calls instead of sleeping."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)
    return calls


def replies(monkeypatch: pytest.MonkeyPatch, *answers: str | type[BaseException]) -> list[str]:
    """Answer ``input()`` prompts in turn, raising any exception type given; return the
    prompts."""
    prompts: list[str] = []
    queue = list(answers)

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        reply = queue.pop(0)
        if isinstance(reply, str):
            return reply
        raise reply

    monkeypatch.setattr(builtins, "input", answer)
    return prompts


def test_replay_prints_every_version_of_a_run(
    finished_run: RunPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    slept = sleeps(monkeypatch)

    app.replay(finished_run.root, auto_advance_s=0)

    assert slept == [0, 0]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Replaying run demo from {finished_run.root}"
    expected = [
        "v1 (first build) scored 55.0%, against a target of 90%: accepted.",
        "  Issues: 2 confirmed, 0 unverifiable, 1 contradicted",
        "    [confirmed] I1 (high, collisions, speedrunner 00:42): "
        "Player passes through the second wall.",
        "    [contradicted] I2 (medium, performance): Frame rate drops in the cave.",
        "  Patch notes: none.",
        f"  Recordings in {finished_run.version(1).playtest_dir}: speedrunner",
        "v2 (fixed from v1) scored 0.0%, against a target of 90%: build failed: it never booted.",
        "  Issues: none, because v2 was not critiqued",
        "  Patch notes:",
        "    - [D1] Rewrite the physics",
        "v3 (fixed from v1) scored 72.5%, against a target of 90%: accepted.",
        "  Issues: 0 confirmed, 0 unverifiable, 0 contradicted",
        "    - [I1, D1] Register the wall colliders",
        "    - [I3] Snappier jumps",
        "End of the replay: 3 versions shown.",
    ]
    for line in expected:
        assert line in lines
    long_issue = next(line for line in lines if "[confirmed] D1" in line)
    assert long_issue.endswith("...")
    assert len(long_issue) < 220


def test_replay_waits_for_enter_before_each_later_version(
    finished_run: RunPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = replies(monkeypatch, "", "")

    app.replay(finished_run.root)

    assert prompts == [
        "\nPress Enter for v2, or Ctrl-D to stop. ",
        "\nPress Enter for v3, or Ctrl-D to stop. ",
    ]
    assert "End of the replay: 3 versions shown." in capsys.readouterr().out


@pytest.mark.parametrize("stop", [EOFError, KeyboardInterrupt])
def test_the_end_of_input_ends_the_replay_cleanly(
    finished_run: RunPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stop: type[BaseException],
) -> None:
    replies(monkeypatch, "", stop)

    app.replay(finished_run.root)

    out = capsys.readouterr().out
    assert "v2 (fixed from v1)" in out
    assert "v3 (fixed from v1)" not in out
    assert "End of the replay: 2 versions shown." in out


def test_ctrl_c_ends_an_automatic_replay_cleanly(
    finished_run: RunPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupted)

    app.replay(finished_run.root, auto_advance_s=5)

    assert "End of the replay: 1 version shown." in capsys.readouterr().out


@pytest.mark.parametrize("seconds", [-1.0, math.inf, math.nan])
def test_replay_refuses_an_impossible_auto_advance(finished_run: RunPaths, seconds: float) -> None:
    with pytest.raises(ValueError, match="auto_advance_s"):
        app.replay(finished_run.root, auto_advance_s=seconds)


def test_replay_needs_a_run(tmp_path: Path) -> None:
    with pytest.raises(RunNotFoundError, match="is not a run"):
        app.replay(tmp_path / "nowhere")


def test_replay_needs_a_finished_version(settings: Settings) -> None:
    run = RunStore(settings.runs_dir).create(run_config("fresh"), DESIGN_DOC)

    with pytest.raises(RunNotFoundError, match="no version has finished"):
        app.replay(run.root, auto_advance_s=0)


def test_replay_stops_at_a_version_without_a_score(
    finished_run: RunPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps(monkeypatch)
    finished_run.version(3).score.unlink()

    with pytest.raises(RunNotFoundError, match="Cannot replay v3"):
        app.replay(finished_run.root, auto_advance_s=0)

    assert "v2 (fixed from v1)" in capsys.readouterr().out
