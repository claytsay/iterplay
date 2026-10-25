"""One full iteration of ``iterplay new`` on the hand-written reference game (HLD §10).

The scripted Builder returns ``examples/reference_game``, so every real stage runs on a
Three.js level: the smoke test and three recorded bot sessions with WebGL in headless
Chromium, ffmpeg media prep, the critic's request, the Verifier and the Scorer. The finished
run then replays from disk, and the viewer page shows it. Only the model is a test double.
"""

import math
import urllib.request
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from iterplay import app
from iterplay.contract.templates import TemplateId
from iterplay.harness.playtest import PlaytestConfig
from iterplay.models.critique import Critique
from iterplay.models.game import BuilderResponse, GameBundle, GameFile
from iterplay.models.playtest import BotName, PlaytestReport, RunEnd
from iterplay.models.run import CriticBackendKind, RunSummary, VersionOutcome
from iterplay.models.scoring import ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.verification import VerificationStatus, VerifiedCritique
from iterplay.settings import Settings
from iterplay.storage.artifacts import read_model
from iterplay.storage.game_files import read_game
from iterplay.storage.layout import RunPaths
from iterplay.storage.telemetry import read_telemetry
from iterplay.viewer.server import viewer_server
from tests.helpers import FakeLLMClient

pytestmark = [pytest.mark.browser, pytest.mark.ffmpeg, pytest.mark.slow]

REPO = Path(__file__).parents[1]
REFERENCE_GAME = REPO / "examples" / "reference_game"
DESIGN_DOC = REPO / "examples" / "tunnel_turbulence.md"
PLAYTEST = PlaytestConfig(max_duration_s=8.0)

CRITIQUE = Critique.model_validate(
    {
        "issues": [
            {
                "id": "I1",
                "bot": "speedrunner",
                "timestamp": "00:02",
                "rubric_item": "game_feel",
                "severity": "medium",
                "observation": "The jump arc looks floatier than the design doc's target.",
                "evidence_type": "visual",
                "suspected_cause": "Gravity is lower than 38 tiles/s² in tuning.js",
                "fix_hint": "Match the feel targets in the design doc",
            }
        ],
        "feel_notes": ["The cave-in reads clearly."],
        "rubric_assessment": {"matches_design_doc": 4, "game_feel": 3},
    }
)


def reference_files() -> list[GameFile]:
    return [
        GameFile(path=path.name, content=path.read_text(encoding="utf-8"))
        for path in sorted(REFERENCE_GAME.iterdir())
        if path.is_file() and not path.name.startswith(".")
    ]


def fetch_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=10) as response:
        status: int = response.status
    return status


def served_by_the_viewer(runs_dir: Path, *paths: str) -> list[int]:
    """The status of each artifact path, fetched through ``iterplay view``'s server."""
    with viewer_server(runs_dir, port=0) as server:
        return [fetch_status(f"{server.base_url}runs/{path}") for path in paths]


async def assert_the_viewer_shows(runs_dir: Path, summary: RunSummary) -> None:
    """The viewer page lists the run's versions with their scores, and v1's critic issue."""
    percents = [f"{math.floor(record.score * 100 + 0.5)}%" for record in summary.versions]
    with viewer_server(runs_dir, port=0) as server:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(f"{server.base_url}#run={summary.run_id}&v=1")
                await expect(page.locator("#run-select")).to_have_value(summary.run_id)
                await expect(page.locator("#versions .version-score")).to_have_text(percents)
                unverifiable = page.locator(
                    '#issue-list .issue-group[data-status="unverifiable"] .issue-id'
                )
                await expect(unverifiable).to_have_text(["I1"])
            finally:
                await browser.close()


async def test_one_iteration_on_the_reference_game(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = reference_files()
    llm = FakeLLMClient({"builder": [BuilderResponse(files=files)], "critic": [CRITIQUE]})
    settings = Settings(runs_dir=tmp_path / "runs").model_copy(update={"game_port": 0})
    options = app.NewRunOptions(
        design_doc=DESIGN_DOC,
        rubric=REPO / "rubric.yaml",
        max_iterations=1,
        critic_backend=CriticBackendKind.DIRECT,
        headless=True,
        template=TemplateId.PLATFORMER_2D,
    )

    summary = await app.new_run(options, settings, llm=llm, playtest=PLAYTEST)

    (record,) = summary.versions
    assert (record.version, record.outcome) == (1, VersionOutcome.ACCEPTED)
    assert summary.best_version == 1
    run = RunPaths(settings.runs_dir / summary.run_id)
    assert summary.run_id.endswith("-tunnel-turbulence")
    assert read_model(run.summary, RunSummary) == summary
    v1 = run.version(1)
    assert read_game(v1.game_dir) == GameBundle(files=files)
    smoke = read_model(v1.smoke_report, SmokeReport)
    assert smoke.passed, smoke

    report = read_model(v1.playtest_report, PlaytestReport)
    assert [result.bot for result in report.runs] == list(BotName)
    for result in report.runs:
        assert result.ended_by is not RunEnd.CRASHED, result
        assert result.uncaught_errors == []
        assert v1.raw_video(result.bot).stat().st_size > 10_000
        assert v1.prepared_video(result.bot).stat().st_size > 1000
    speedrunner = read_telemetry(v1.telemetry(BotName.SPEEDRUNNER))
    assert speedrunner[-1].state.player.x - speedrunner[0].state.player.x > 5
    assert v1.combined_video.stat().st_size > 10_000

    (request,) = llm.requests_for("critic")
    assert [media.path for media in request.media] == [v1.combined_video]
    assert "Tunnel Turbulence" in request.prompt
    verified = read_model(v1.critique_verified, VerifiedCritique)
    assert verified.issues[0].issue.id == "I1"
    assert verified.issues[0].status is VerificationStatus.UNVERIFIABLE
    score = read_model(v1.score, ScoreCard)
    assert score.total == record.score
    items = {item.item_id: item.score for item in score.items}
    assert items["boots_cleanly"] == 1.0
    assert items["no_runtime_errors"] == 1.0
    assert items["performance"] is None
    assert items["matches_design_doc"] == 0.8

    app.replay(run.root, auto_advance_s=0)
    out = capsys.readouterr().out
    assert f"v1 (first build) scored {record.score:.1%}" in out
    assert "[unverifiable] I1 (medium, game_feel, speedrunner 00:02)" in out
    assert served_by_the_viewer(
        settings.runs_dir,
        "index.json",
        f"{summary.run_id}/summary.json",
        f"{summary.run_id}/v1/critique.verified.json",
        f"{summary.run_id}/v1/playtest/speedrunner.webm",
    ) == [200, 200, 200, 200]
    await assert_the_viewer_shows(settings.runs_dir, summary)
