"""The CLI accepts every command documented in HLD §7."""

from pathlib import Path

import pytest

from iterplay.cli import build_parser
from iterplay.contract.templates import TemplateId
from iterplay.models.run import CriticBackendKind


def test_new_command_matches_hld() -> None:
    args = build_parser().parse_args(
        ["new", "--doc", "examples/tunnel_turbulence.md", "--rubric", "rubric.yaml", "--iters", "5"]
    )

    assert args.command == "new"
    assert args.doc == Path("examples/tunnel_turbulence.md")
    assert args.rubric == Path("rubric.yaml")
    assert args.iters == 5
    assert args.critic is CriticBackendKind.DIRECT
    assert args.template is TemplateId.PLATFORMER_2D
    assert args.headed is False


@pytest.mark.parametrize(
    ("argv", "command"),
    [(["play", "runs/abc/v3"], "play"), (["replay", "runs/abc"], "replay"), (["view"], "view")],
)
def test_other_commands_match_hld(argv: list[str], command: str) -> None:
    assert build_parser().parse_args(argv).command == command


@pytest.mark.parametrize("iters", ["0", "-1", "five"])
def test_new_rejects_invalid_iteration_counts(iters: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["new", "--doc", "doc.md", "--iters", iters])
