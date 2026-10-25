"""Run directories follow the layout in HLD §6."""

from pathlib import Path

from iterplay.models.playtest import BotName
from iterplay.storage.layout import RunPaths


def test_layout_matches_hld(tmp_path: Path) -> None:
    run = RunPaths(tmp_path / "abc123")
    v2 = run.version(2)
    paths = {
        "config": run.config,
        "design_doc": run.design_doc,
        "summary": run.summary,
        "game": v2.game_dir,
        "smoke": v2.smoke_report,
        "video": v2.raw_video(BotName.SPEEDRUNNER),
        "telemetry": v2.telemetry(BotName.RANDOM),
        "console": v2.console_log,
        "combined": v2.combined_video,
        "critique_raw": v2.critique_raw,
        "critique_verified": v2.critique_verified,
        "score": v2.score,
        "changelog": v2.changelog,
        "llm": v2.llm_dir,
    }

    assert run.run_id == "abc123"
    assert {name: path.relative_to(run.root).as_posix() for name, path in paths.items()} == {
        "config": "config.json",
        "design_doc": "design_doc.md",
        "summary": "summary.json",
        "game": "v2/game",
        "smoke": "v2/smoke.json",
        "video": "v2/playtest/speedrunner.webm",
        "telemetry": "v2/playtest/random.telemetry.jsonl",
        "console": "v2/playtest/console.log",
        "combined": "v2/playtest/combined_360p.mp4",
        "critique_raw": "v2/critique.raw.json",
        "critique_verified": "v2/critique.verified.json",
        "score": "v2/score.json",
        "changelog": "v2/changelog.md",
        "llm": "v2/llm",
    }
