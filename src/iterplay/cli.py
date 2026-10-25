"""The ``iterplay`` command line (HLD §7). Parsing only: the work happens in :mod:`iterplay.app`."""

import argparse
import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from iterplay import __version__, app
from iterplay.contract.templates import DEFAULT_TEMPLATE, TEMPLATES, TemplateId
from iterplay.models.run import CriticBackendKind
from iterplay.settings import load_settings

DEFAULT_ITERATIONS: Final = 5
DEFAULT_RUBRIC: Final = Path("rubric.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iterplay",
        description="Turn a design doc into a browser game, then improve it automatically.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    new = commands.add_parser("new", help="generate a game from a design doc and improve it")
    new.add_argument("--doc", type=Path, required=True, help="design doc (Markdown)")
    new.add_argument(
        "--rubric", type=Path, default=DEFAULT_RUBRIC, help="scoring rubric (default: %(default)s)"
    )
    new.add_argument(
        "--iters",
        type=_positive_int,
        default=DEFAULT_ITERATIONS,
        help="maximum number of versions to build (default: %(default)s)",
    )
    new.add_argument(
        "--critic",
        type=CriticBackendKind,
        choices=list(CriticBackendKind),
        default=CriticBackendKind.DIRECT,
        help="critic backend (default: %(default)s)",
    )
    new.add_argument(
        "--template",
        type=TemplateId,
        choices=list(TEMPLATES),
        default=DEFAULT_TEMPLATE,
        help="kind of game to build (default: %(default)s)",
    )
    new.add_argument(
        "--headed", action="store_true", help="run browsers headed (GPU) to score performance"
    )
    new.add_argument("--run-id", help="name of the run directory (default: generated)")
    new.set_defaults(handler=_new)

    play = commands.add_parser("play", help="open a version in your browser to play by hand")
    play.add_argument("version_dir", type=Path, help="runs/<id>/vN, or a game directory")
    play.add_argument("--seed", type=int, default=1, help="level seed (default: %(default)s)")
    play.set_defaults(handler=_play)

    replay = commands.add_parser(
        "replay", help="step through a finished run without calling the API"
    )
    replay.add_argument("run_dir", type=Path, help="runs/<id>")
    replay.add_argument(
        "--auto",
        type=float,
        metavar="SECONDS",
        help="advance every SECONDS instead of waiting for Enter",
    )
    replay.set_defaults(handler=_replay)

    view = commands.add_parser("view", help="serve the viewer on 127.0.0.1")
    view.set_defaults(handler=_view)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


def _new(args: argparse.Namespace) -> int:
    options = app.NewRunOptions(
        design_doc=args.doc,
        rubric=args.rubric,
        max_iterations=args.iters,
        critic_backend=args.critic,
        headless=not args.headed,
        template=args.template,
        run_id=args.run_id,
    )
    asyncio.run(app.new_run(options, load_settings()))
    return 0


def _play(args: argparse.Namespace) -> int:
    app.play(args.version_dir, load_settings(), seed=args.seed)
    return 0


def _replay(args: argparse.Namespace) -> int:
    app.replay(args.run_dir, auto_advance_s=args.auto)
    return 0


def _view(args: argparse.Namespace) -> int:
    app.view(load_settings())
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value
