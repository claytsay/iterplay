"""The viewer page (HLD §7) in Chromium: run selector, version timeline and sparkline, v1 and
the selected version side by side, verified issues with seekable timestamps, and patch notes
rendered without letting markup from the artifacts into the page.

The runs directory is built with the models and storage helpers, served by ``viewer_server``
on 127.0.0.1, and driven with Playwright. Videos are tiny ffmpeg ``testsrc`` clips, made only
when ffmpeg is installed; the tests that play them are marked ``ffmpeg``.
"""

import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Locator,
    Page,
    expect,
    sync_playwright,
)

from iterplay.models.critique import EvidenceType, Issue, Severity
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.run import (
    RunConfig,
    RunSummary,
    StopReason,
    TokenUsage,
    VersionOutcome,
    VersionRecord,
)
from iterplay.models.scoring import ItemScore, ScoreCard
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)
from iterplay.serving import LoopbackHTTPServer
from iterplay.storage.artifacts import write_model, write_text
from iterplay.storage.layout import RunPaths
from iterplay.storage.run_store import RunStore
from iterplay.viewer.server import viewer_server

pytestmark = pytest.mark.browser

DEMO: Final = "2026-10-25-demo"
"""The latest run: two versions with every artifact."""

BROKEN: Final = "2026-10-24-broken"
"""Its summary.json is not JSON."""

PENDING: Final = "2026-10-24-pending"
"""Still building v1, so it has no summary.json."""

OLD: Final = "2026-10-23-old"
"""Two versions with nothing but an unfinished summary; v2 failed to build."""

LATEST_FIRST: Final = [DEMO, BROKEN, PENDING, OLD]

RUBRIC: Final = Rubric(
    target=0.9,
    items=[
        RubricItem(id="boots_cleanly", description="Boots.", source=RubricSource.SMOKE, weight=15),
        RubricItem(
            id="completable", description="Completes.", source=RubricSource.TELEMETRY, weight=20
        ),
        RubricItem(id="game_feel", description="Feels.", source=RubricSource.CRITIC, weight=5),
    ],
)

XSS_IMAGE: Final = '<img src=x onerror="window.__xss = 1">'
XSS_SCRIPT: Final = "<script>window.__xss = 2</script>"

CHANGELOG_V2: Final = f"""# v2 patch notes

- **I1**: Registered the wall meshes with the collision system in `level.js`.
  - Walls now stop the player {XSS_SCRIPT}
- D1: Guarded the HUD against a missing <b>score</b> element.
  It no longer throws.

1. Rebuilt the level
2. Retuned the jump

A plain paragraph with <i>markup</i>.

---

````js
const wall = "<b>not bold</b>";
```
- not a list
````
"""

FENCED_CODE: Final = 'const wall = "<b>not bold</b>";\n```\n- not a list\n'

HIDDEN_404: Final = "Failed to load resource: the server responded with a status of 404"
"""What Chromium logs for every missing artifact; the page handles those itself."""


class Viewer:
    """Opens the viewer in fresh browser contexts and records every error they report."""

    def __init__(self, browser: Browser, server: LoopbackHTTPServer) -> None:
        self._browser = browser
        self._server = server
        self._contexts: list[BrowserContext] = []
        self.errors: list[str] = []

    def open(self, fragment: str = "", *, server: LoopbackHTTPServer | None = None) -> Page:
        context = self._browser.new_context()
        self._contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(f"uncaught: {error}"))
        page.on("console", self._record)
        page.goto((server or self._server).base_url + fragment)
        return page

    def close(self) -> None:
        for context in self._contexts:
            context.close()

    def _record(self, message: ConsoleMessage) -> None:
        if message.type == "error" and not message.text.startswith(HIDDEN_404):
            self.errors.append(message.text)


# Fixtures


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    runs = tmp_path_factory.mktemp("viewer") / "runs"
    store = RunStore(runs)
    clip = _make_clip(tmp_path_factory.mktemp("clip") / "clip.webm")
    _write_demo_run(store.create(_config(DEMO, day=25), "# Demo\n"), clip)
    _write_old_run(store.create(_config(OLD, day=23), "# Old\n"))
    store.create(_config(PENDING, day=24, hour=8), "# Pending\n")
    write_text(store.create(_config(BROKEN, day=24, hour=9), "# Broken\n").summary, "{not json")
    store.write_index()
    return runs


@pytest.fixture(scope="module")
def server(runs_dir: Path) -> Iterator[LoopbackHTTPServer]:
    with viewer_server(runs_dir, port=0) as running:
        yield running


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch()
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def viewer(browser: Browser, server: LoopbackHTTPServer) -> Iterator[Viewer]:
    opened = Viewer(browser, server)
    try:
        yield opened
    finally:
        opened.close()


def _config(run_id: str, *, day: int, hour: int = 12) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        created_at=datetime(2026, 10, day, hour, tzinfo=UTC),
        design_doc_sha256="0" * 64,
        rubric=RUBRIC,
        model="gemini-4-argon",
        bot_seeds={bot: seed for seed, bot in enumerate(BotName, start=1)},
    )


def _record(version: int, score: float, outcome: VersionOutcome) -> VersionRecord:
    return VersionRecord(
        version=version,
        parent_version=None if version == 1 else version - 1,
        score=score,
        outcome=outcome,
        duration_s=87.5,
        token_usage=TokenUsage(input_tokens=1500, output_tokens=2500),
    )


def _score_card(version: int, total: float, scores: tuple[float, float, float | None]) -> ScoreCard:
    items = [
        ItemScore(item_id=item.id, source=item.source, weight=item.weight, score=score)
        for item, score in zip(RUBRIC.items, scores, strict=True)
    ]
    return ScoreCard(
        version=version,
        items=items,
        total=total,
        target=RUBRIC.target,
        meets_target=total >= RUBRIC.target,
    )


def _issue(
    issue_id: str,
    status: VerificationStatus,
    severity: Severity,
    rubric_item: str,
    observation: str,
    rationale: str,
    *,
    bot: BotName | None = None,
    timestamp: str | None = None,
    origin: IssueOrigin = IssueOrigin.CRITIC,
) -> VerifiedIssue:
    issue = Issue(
        id=issue_id,
        bot=bot,
        timestamp=timestamp,
        rubric_item=rubric_item,
        severity=severity,
        observation=observation,
        evidence_type=EvidenceType.VISUAL_TELEMETRY,
    )
    return VerifiedIssue(issue=issue, origin=origin, status=status, rationale=rationale)


def _write_demo_run(run: RunPaths, clip: Path | None) -> None:
    write_model(
        run.summary,
        RunSummary(
            run_id=run.run_id,
            versions=[
                _record(1, 0.35, VersionOutcome.ACCEPTED),
                _record(2, 0.92, VersionOutcome.ACCEPTED),
            ],
            best_version=2,
            stop_reason=StopReason.TARGET_REACHED,
            duration_s=175,
            token_usage=TokenUsage(input_tokens=3000, output_tokens=5000),
            spend_usd=0.12,
        ),
    )
    v1, v2 = run.version(1), run.version(2)
    write_model(v1.score, _score_card(1, 0.35, (1, 0, None)))
    write_model(v2.score, _score_card(2, 0.92, (1, 1, 0.6)))
    write_model(
        v1.critique_verified,
        VerifiedCritique(
            issues=[
                _issue(
                    "I1",
                    VerificationStatus.CONFIRMED,
                    Severity.HIGH,
                    "collision_integrity",
                    "The player falls through the floor.",
                    "An oob event at 00:02 matches the claim.",
                    bot=BotName.SPEEDRUNNER,
                    timestamp="00:02",
                )
            ]
        ),
    )
    write_model(v2.critique_verified, _demo_v2_critique())
    write_text(v1.changelog, "")
    write_text(v2.changelog, CHANGELOG_V2)
    if clip is not None:
        for version in (v1, v2):
            for bot in (BotName.RANDOM, BotName.SPEEDRUNNER):
                version.playtest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(clip, version.raw_video(bot))


def _demo_v2_critique() -> VerifiedCritique:
    return VerifiedCritique(
        issues=[
            _issue(
                "D1",
                VerificationStatus.CONFIRMED,
                Severity.MEDIUM,
                "no_runtime_errors",
                "Uncaught TypeError: <b>hud</b> is undefined",
                "Seen in the console of 1 run.",
                origin=IssueOrigin.DETERMINISTIC,
            ),
            _issue(
                "I2",
                VerificationStatus.UNVERIFIABLE,
                Severity.LOW,
                "game_feel",
                "Jumps feel floaty.",
                "Subjective feel: telemetry cannot settle it.",
                bot=BotName.NEWBIE,
                timestamp="00:02",
            ),
            _issue(
                "I3",
                VerificationStatus.CONTRADICTED,
                Severity.CRITICAL,
                "completable",
                f"The level cannot be completed {XSS_IMAGE}",
                "The Speedrunner completed the level at 00:41.",
                bot=BotName.RANDOM,
                timestamp="00:03",
            ),
            _issue(
                "I1",
                VerificationStatus.CONFIRMED,
                Severity.HIGH,
                "collision_integrity",
                "The player clips through the second wall.",
                "An oob event at 00:01 matches the claim.",
                bot=BotName.SPEEDRUNNER,
                timestamp="00:01",
            ),
        ],
        feel_notes=["The jump arc is floatier than the design doc's 'snappy' target."],
    )


def _write_old_run(run: RunPaths) -> None:
    write_model(
        run.summary,
        RunSummary(
            run_id=run.run_id,
            versions=[
                _record(1, 0.5, VersionOutcome.ACCEPTED),
                _record(2, 0.0, VersionOutcome.BUILD_FAILED),
            ],
            best_version=1,
        ),
    )


def _make_clip(path: Path) -> Path | None:
    """A 4 s, 10 fps webm test pattern, or None when ffmpeg is not installed."""
    if shutil.which("ffmpeg") is None:
        return None
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
            "testsrc=duration=4:size=160x90:rate=10",
            "-c:v",
            "libvpx",
            "-b:v",
            "200k",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


def _version_button(version: int) -> str:
    return f'#versions button[data-version="{version}"]'


def _issue_item(page: Page, issue_id: str) -> Locator:
    return page.locator("#issue-list .issue").filter(has=page.get_by_text(issue_id, exact=True))


def _number(page: Page, selector: str, attribute: str) -> float:
    value = page.locator(selector).get_attribute(attribute)
    assert value is not None, f"{selector} has no {attribute}"
    return float(value)


def _wait_for_metadata(page: Page, video: str) -> None:
    page.wait_for_function(
        "(id) => document.getElementById(id).readyState >= HTMLMediaElement.HAVE_METADATA",
        arg=video,
    )


def _wait_for_current_time(page: Page, video: str, seconds: float) -> None:
    page.wait_for_function(
        "([id, seconds]) => Math.abs(document.getElementById(id).currentTime - seconds) < 0.1",
        arg=[video, seconds],
    )


# The run selector


def test_runs_are_listed_latest_first_and_the_latest_is_shown(viewer: Viewer) -> None:
    page = viewer.open()

    expect(page.locator("#run-select option")).to_have_text(LATEST_FIRST)
    expect(page.locator("#run-select")).to_have_value(DEMO)
    expect(page.locator("#run-meta")).to_have_text(
        "2 versions · best v2 (92%) · stopped: target reached · 2m 55s · 8,000 tokens · $0.12"
    )
    expect(page).to_have_title(f"{DEMO} · iterplay viewer")
    assert viewer.errors == []


def test_choosing_a_run_shows_it(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator("#versions .version-score")).to_have_text(["35%", "92%"])

    page.select_option("#run-select", OLD)

    expect(page.locator("#versions .version-score")).to_have_text(["50%", "0%"])
    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#run-meta")).to_have_text("2 versions · best v1 (50%) · not finished")
    expect(page).to_have_title(f"{OLD} · iterplay viewer")
    expect(page).to_have_url(re.compile(rf"#run={OLD}&v=1&bot=speedrunner$"))


# Version timeline and sparkline


def test_the_timeline_shows_every_version_with_its_score(viewer: Viewer) -> None:
    page = viewer.open()
    buttons = page.locator("#versions button.version")

    expect(buttons.locator(".version-label")).to_have_text(["v1", "v2"])
    expect(buttons.locator(".version-score")).to_have_text(["35%", "92%"])
    expect(buttons.locator(".version-outcome")).to_have_text(["accepted", "accepted"])
    expect(buttons.nth(0)).to_contain_class("outcome-accepted")
    expect(buttons.nth(1)).to_contain_class("best")
    expect(buttons.nth(0)).not_to_contain_class("best")
    expect(buttons.nth(1).locator(".version-best")).to_have_text("★ best")
    expect(buttons.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(buttons.nth(0)).to_have_attribute("aria-pressed", "false")
    expect(buttons.nth(1)).to_have_attribute(
        "title", "v2: 92%, accepted, fixed from v1, took 1m 28s"
    )


def test_the_timeline_styles_each_outcome(viewer: Viewer) -> None:
    page = viewer.open(f"#run={OLD}")
    failed = page.locator(_version_button(2))

    expect(failed).to_contain_class("outcome-build_failed")
    expect(failed.locator(".version-outcome")).to_have_text("build failed")
    expect(page.locator(_version_button(1))).to_contain_class("best")


def test_the_sparkline_plots_each_score_against_the_target(viewer: Viewer) -> None:
    page = viewer.open()
    svg = page.locator("#sparkline svg.sparkline")

    expect(svg.locator("circle.point")).to_have_count(2)
    expect(svg.locator("polyline.scores")).to_have_count(1)
    expect(svg.locator("line.target")).to_have_count(1)
    expect(svg.locator("text.target-label")).to_have_text("target 90%")
    expect(svg.locator("circle.point.selected")).to_have_attribute("data-version", "2")
    expect(svg).to_have_attribute("aria-label", "Score per version: v1 35%, v2 92%. Target 90%.")
    target_y = _number(page, "#sparkline line.target", "y1")
    v1_y = _number(page, '#sparkline circle[data-version="1"]', "cy")
    v2_y = _number(page, '#sparkline circle[data-version="2"]', "cy")
    assert v2_y < target_y < v1_y, "92% plots above the 90% target and 35% below it"


def test_clicking_a_sparkline_point_selects_its_version(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator("#sparkline line.target")).to_have_count(1)

    page.locator('#sparkline circle[data-version="1"]').click()

    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#sparkline circle.point.selected")).to_have_attribute("data-version", "1")


def test_arrow_keys_step_through_the_versions(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")

    page.keyboard.press("ArrowLeft")

    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    expect(page.locator(_version_button(1))).to_be_focused()
    page.keyboard.press("ArrowLeft")
    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("ArrowRight")
    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")


# The URL hash


def test_the_hash_keeps_the_selection_across_reloads(viewer: Viewer) -> None:
    page = viewer.open(f"#run={DEMO}&v=1&bot=random")

    expect(page.locator("#run-select")).to_have_value(DEMO)
    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#bot-select")).to_have_value("random")
    expect(page.locator("#slot-current figcaption")).to_have_text("v1 · Random")

    page.locator(_version_button(2)).click()
    expect(page).to_have_url(re.compile(rf"#run={DEMO}&v=2&bot=random$"))
    page.reload()

    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#bot-select")).to_have_value("random")
    expect(page.locator("#slot-current figcaption")).to_have_text("v2 · Random · best")


def test_editing_the_hash_changes_the_selection(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")

    page.evaluate(f"location.hash = '#run={DEMO}&v=1&bot=newbie'")

    expect(page.locator(_version_button(1))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#bot-select")).to_have_value("newbie")
    page.evaluate(f"location.hash = '#run={OLD}&v=2'")
    expect(page.locator("#run-select")).to_have_value(OLD)
    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")


def test_an_unknown_run_or_version_in_the_hash_falls_back_to_the_latest_best(
    viewer: Viewer,
) -> None:
    page = viewer.open("#run=no-such-run&v=9&bot=robot")

    expect(page.locator("#run-select")).to_have_value(DEMO)
    expect(page.locator(_version_button(2))).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#bot-select")).to_have_value("speedrunner")
    expect(page).to_have_url(re.compile(rf"#run={DEMO}&v=2&bot=speedrunner$"))


# Videos


def test_v1_and_the_selected_version_are_shown_side_by_side(viewer: Viewer) -> None:
    page = viewer.open()

    expect(page.locator("#bot-select option")).to_have_text(["Random", "Speedrunner", "Newbie"])
    expect(page.locator("#slot-first figcaption")).to_have_text("v1 · Speedrunner")
    expect(page.locator("#slot-current figcaption")).to_have_text("v2 · Speedrunner · best")
    page.locator(_version_button(1)).click()
    expect(page.locator("#slot-current figcaption")).to_have_text("v1 · Speedrunner")


@pytest.mark.ffmpeg
def test_the_videos_are_each_versions_recording_of_the_chosen_bot(viewer: Viewer) -> None:
    page = viewer.open()
    first, current = page.locator("#video-first"), page.locator("#video-current")

    expect(first).to_have_attribute("src", f"runs/{DEMO}/v1/playtest/speedrunner.webm")
    expect(current).to_have_attribute("src", f"runs/{DEMO}/v2/playtest/speedrunner.webm")
    expect(current).to_be_visible()
    expect(page.locator("#slot-current .placeholder")).to_be_hidden()
    page.select_option("#bot-select", "random")

    expect(first).to_have_attribute("src", f"runs/{DEMO}/v1/playtest/random.webm")
    expect(current).to_have_attribute("src", f"runs/{DEMO}/v2/playtest/random.webm")
    expect(page).to_have_url(re.compile(r"&bot=random$"))
    assert viewer.errors == []


@pytest.mark.ffmpeg
def test_play_both_starts_and_pauses_both_videos(viewer: Viewer) -> None:
    page = viewer.open()
    videos = page.locator("#videos video")
    _wait_for_metadata(page, "video-first")
    _wait_for_metadata(page, "video-current")

    page.locator("#play-both").click()

    expect(videos.nth(0)).to_have_js_property("paused", False)
    expect(videos.nth(1)).to_have_js_property("paused", False)
    expect(page.locator("#play-both")).to_have_text("Pause both")
    page.locator("#play-both").click()
    expect(videos.nth(0)).to_have_js_property("paused", True)
    expect(videos.nth(1)).to_have_js_property("paused", True)
    expect(page.locator("#play-both")).to_have_text("Play both")


def test_a_missing_video_shows_a_placeholder(viewer: Viewer) -> None:
    page = viewer.open(f"#run={DEMO}&bot=newbie")

    expect(page.locator("#slot-first .placeholder")).to_have_text("No Newbie video for v1.")
    expect(page.locator("#slot-current .placeholder")).to_have_text("No Newbie video for v2.")
    expect(page.locator("#video-first")).to_be_hidden()
    expect(page.locator("#video-current")).to_be_hidden()
    assert page.locator("#video-current").get_attribute("src") is None
    assert viewer.errors == []


# Verified issues


def test_issues_are_grouped_by_verification_status(viewer: Viewer) -> None:
    page = viewer.open()
    groups = page.locator("#issue-list .issue-group")

    expect(groups.locator("h3")).to_have_text(
        ["Confirmed (2)", "Unverifiable (1)", "Caught hallucinations (1)"]
    )
    expect(groups.nth(0).locator(".issue-id")).to_have_text(["I1", "D1"])
    expect(groups.nth(1).locator(".issue-id")).to_have_text(["I2"])
    expect(page.locator("#issue-list .feel-notes li")).to_have_text(
        ["The jump arc is floatier than the design doc's 'snappy' target."]
    )


def test_a_contradicted_claim_is_shown_as_a_caught_hallucination(viewer: Viewer) -> None:
    page = viewer.open()
    group = page.locator('#issue-list .issue-group[data-status="contradicted"]')
    issue = group.locator(".issue")

    expect(group.locator("h3")).to_have_text("Caught hallucinations (1)")
    expect(issue).to_have_count(1)
    expect(issue.locator(".issue-id")).to_have_text("I3")
    expect(issue.locator(".issue-severity")).to_have_text("critical")
    expect(issue.locator(".issue-rubric")).to_have_text("completable")
    expect(issue.locator(".issue-bot")).to_have_text("Random")
    expect(issue.locator(".issue-timestamp")).to_have_text("00:03")
    expect(issue.locator(".issue-observation")).to_have_text(
        f"The level cannot be completed {XSS_IMAGE}"
    )
    expect(issue.locator(".issue-rationale")).to_have_text(
        "Verifier: The Speedrunner completed the level at 00:41."
    )


def test_each_issue_shows_its_severity_rubric_item_and_origin(viewer: Viewer) -> None:
    page = viewer.open()
    deterministic = _issue_item(page, "D1")

    expect(deterministic).to_contain_class("severity-medium")
    expect(deterministic.locator(".issue-severity")).to_have_text("medium")
    expect(deterministic.locator(".issue-rubric")).to_have_text("no_runtime_errors")
    expect(deterministic.locator(".issue-origin")).to_have_text("deterministic")
    expect(deterministic.locator(".issue-timestamp")).to_have_count(0)
    expect(deterministic.locator(".issue-observation")).to_have_text(
        "Uncaught TypeError: <b>hud</b> is undefined"
    )


@pytest.mark.ffmpeg
def test_clicking_a_timestamp_selects_its_bot_and_seeks_the_video(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator("#bot-select")).to_have_value("speedrunner")

    page.locator('[data-status="contradicted"] .issue-timestamp').click()

    expect(page.locator("#bot-select")).to_have_value("random")
    expect(page.locator("#video-current")).to_have_attribute(
        "src", f"runs/{DEMO}/v2/playtest/random.webm"
    )
    _wait_for_current_time(page, "video-current", 3)
    expect(page).to_have_url(re.compile(r"&bot=random$"))

    _issue_item(page, "I1").locator(".issue-timestamp").click()

    expect(page.locator("#bot-select")).to_have_value("speedrunner")
    _wait_for_current_time(page, "video-current", 1)
    assert page.evaluate("document.getElementById('video-first').currentTime") == 0


@pytest.mark.ffmpeg
def test_a_timestamp_seeks_the_already_loaded_video_of_its_bot(viewer: Viewer) -> None:
    page = viewer.open()
    _wait_for_metadata(page, "video-current")

    _issue_item(page, "I1").locator(".issue-timestamp").click()

    _wait_for_current_time(page, "video-current", 1)
    expect(page.locator("#bot-select")).to_have_value("speedrunner")


# Patch notes


def test_patch_notes_render_minimal_markdown(viewer: Viewer) -> None:
    page = viewer.open()
    notes = page.locator("#patch-notes-body .markdown")

    expect(notes.locator("h3")).to_have_text("v2 patch notes")
    expect(notes.locator(":scope > ul > li")).to_have_count(2)
    expect(notes.locator(":scope > ul > li").nth(1)).to_have_text(
        "D1: Guarded the HUD against a missing <b>score</b> element. It no longer throws."
    )
    expect(notes.locator(":scope > ul > li strong")).to_have_text("I1")
    expect(notes.locator(":scope > ul > li code")).to_have_text("level.js")
    expect(notes.locator("ul ul > li")).to_have_text(f"Walls now stop the player {XSS_SCRIPT}")
    expect(notes.locator("ol > li")).to_have_text(["Rebuilt the level", "Retuned the jump"])
    expect(notes.locator(":scope > p")).to_have_text("A plain paragraph with <i>markup</i>.")
    expect(notes.locator(":scope > hr")).to_have_count(1)
    assert notes.locator(":scope > pre > code").text_content() == FENCED_CODE


def test_markup_inside_artifacts_is_shown_as_text(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator("#patch-notes-body .markdown")).to_contain_text("<b>score</b>")
    expect(page.locator("#issue-list")).to_contain_text(XSS_IMAGE)

    for tag in ("script", "img", "b", "i"):
        expect(page.locator(f"main {tag}")).to_have_count(0)
    assert page.evaluate("window.__xss") is None
    assert viewer.errors == []


def test_the_content_security_policy_refuses_inline_scripts(viewer: Viewer) -> None:
    page = viewer.open()
    expect(page.locator("#versions button.version")).to_have_count(2)

    violated = page.evaluate(
        """() => new Promise((resolve) => {
          document.addEventListener(
            "securitypolicyviolation", (event) => resolve(event.effectiveDirective), {once: true}
          );
          const script = document.createElement("script");
          script.textContent = "window.__inline = 1";
          document.body.append(script);
        })"""
    )

    assert violated == "script-src-elem"
    assert page.evaluate("window.__inline") is None


def test_v1_has_no_patch_notes(viewer: Viewer) -> None:
    page = viewer.open(f"#run={DEMO}&v=1")

    expect(page.locator("#patch-notes-body")).to_have_text(
        "v1 was built from the design doc, so it has no patch notes."
    )
    expect(page.locator("#issue-list .issue-id")).to_have_text(["I1"])


# Score breakdown


def test_the_score_breakdown_lists_every_rubric_item(viewer: Viewer) -> None:
    page = viewer.open()
    rows = page.locator("#score-body tbody tr")

    expect(page.locator("#score-body .score-total")).to_have_text(
        "Total 92% against a target of 90%: it meets the target."
    )
    expect(rows.locator("td:first-child")).to_have_text(
        ["boots_cleanly", "completable", "game_feel"]
    )
    expect(rows.locator("td:nth-child(4)")).to_have_text(["100%", "100%", "60%"])
    page.locator(_version_button(1)).click()
    expect(rows.locator("td:nth-child(4)")).to_have_text(["100%", "0%", "not measured"])
    expect(rows.nth(2)).to_contain_class("item-unmeasured")
    expect(page.locator("#score-body .score-total")).to_have_text(
        "Total 35% against a target of 90%: it is below the target."
    )


# Missing and broken artifacts


def test_missing_artifacts_show_messages(viewer: Viewer) -> None:
    page = viewer.open(f"#run={OLD}")

    expect(page.locator("#score-body")).to_have_text("No score breakdown: v1 has no score.json.")
    expect(page.locator("#issue-list")).to_have_text(
        "No verified issues: v1 has no critique.verified.json."
    )
    expect(page.locator("#patch-notes-body")).to_have_text(
        "v1 was built from the design doc, so it has no patch notes."
    )
    expect(page.locator("#slot-first .placeholder")).to_have_text("No Speedrunner video for v1.")
    expect(page.locator("#sparkline circle.point")).to_have_count(2)
    expect(page.locator("#sparkline line.target")).to_have_count(0)

    page.locator(_version_button(2)).click()

    expect(page.locator("#issue-list")).to_have_text("v2 never booted, so it was not critiqued.")
    expect(page.locator("#patch-notes-body")).to_have_text(
        "No patch notes: v2 has no changelog.md."
    )
    assert viewer.errors == []


def test_a_run_without_a_summary_shows_a_message(viewer: Viewer) -> None:
    page = viewer.open(f"#run={PENDING}")

    expect(page.locator("#status")).to_have_text(
        f"{PENDING} has no summary.json yet, so no version has finished. Reload later."
    )
    expect(page.locator("#run")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#run={PENDING}&bot=speedrunner$"))

    page.select_option("#run-select", DEMO)

    expect(page.locator("#versions button.version")).to_have_count(2)
    expect(page.locator("#status")).to_be_hidden()
    assert viewer.errors == []


def test_a_corrupt_summary_shows_an_error(viewer: Viewer) -> None:
    page = viewer.open(f"#run={BROKEN}")
    status = page.locator("#status")

    expect(status).to_contain_text(f"runs/{BROKEN}/summary.json is not valid JSON")
    expect(status).to_contain_class("error")
    expect(page.locator("#run")).to_be_hidden()
    expect(page.locator("#run-select")).to_be_enabled()


def test_without_an_index_the_page_says_there_are_no_runs(viewer: Viewer, tmp_path: Path) -> None:
    with viewer_server(tmp_path / "runs", port=0) as empty:
        page = viewer.open(server=empty)

        expect(page.locator("#status")).to_contain_text(
            "No runs to show: runs/index.json was not found."
        )
        expect(page.locator("#run-select")).to_be_disabled()
        expect(page.locator("#run")).to_be_hidden()
    assert viewer.errors == []


def test_an_empty_index_says_there_are_no_runs_yet(viewer: Viewer, tmp_path: Path) -> None:
    RunStore(tmp_path / "runs").write_index()

    with viewer_server(tmp_path / "runs", port=0) as empty:
        page = viewer.open(server=empty)

        expect(page.locator("#status")).to_have_text(
            "No runs yet. Create one with iterplay new, then restart iterplay view."
        )
    assert viewer.errors == []


@pytest.mark.parametrize("index", ['{"runs": ["r1"]}', "[]", "{not json"])
def test_a_malformed_index_shows_an_error(viewer: Viewer, tmp_path: Path, index: str) -> None:
    write_text(tmp_path / "runs" / "index.json", index)

    with viewer_server(tmp_path / "runs", port=0) as malformed:
        page = viewer.open(server=malformed)
        status = page.locator("#status")

        expect(status).to_contain_text("runs/index.json is not")
        expect(status).to_contain_class("error")
        expect(page.locator("#run-select")).to_be_disabled()
    assert viewer.errors == []


def test_browsing_every_run_version_and_bot_raises_no_errors(viewer: Viewer) -> None:
    page = viewer.open(f"#run={OLD}")
    unavailable = {BROKEN: "is not valid JSON", PENDING: "has no summary.json yet"}
    loaded = re.compile(r"^(?!Loading)")

    for run_id in LATEST_FIRST:
        page.select_option("#run-select", run_id)
        if run_id in unavailable:
            expect(page.locator("#status")).to_contain_text(unavailable[run_id])
            continue
        expect(page.locator("#run")).to_be_visible()
        for version in (1, 2):
            page.locator(_version_button(version)).click()
            for bot in ("random", "newbie", "speedrunner"):
                page.select_option("#bot-select", bot)
                expect(page.locator("#slot-current .placeholder")).to_have_text(loaded)
            expect(page.locator("#issue-list")).to_have_text(loaded)
            expect(page.locator("#patch-notes-body")).to_have_text(loaded)
            expect(page.locator("#score-body")).to_have_text(loaded)

    assert viewer.errors == []
