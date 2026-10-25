"""The use case behind each CLI command, and the composition root.

This is the only module that chooses concrete implementations (Gemini, Playwright, ffmpeg);
everything else receives its collaborators through constructors.
"""

import hashlib
import logging
import math
import re
import shutil
import socket
import time
import unicodedata
import webbrowser
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeAlias

from pydantic import ValidationError

from iterplay.agents.builder import Builder
from iterplay.agents.critic.base import CriticBackend
from iterplay.agents.critic.direct import DirectCalls
from iterplay.agents.critic.managed import ManagedAgents
from iterplay.agents.fixer import Fixer
from iterplay.agents.prompt_library import PromptLibrary
from iterplay.contract.controls import ControlScheme
from iterplay.contract.layout import ENTRY_HTML, GAME_DIR
from iterplay.contract.templates import TEMPLATES, TemplateId
from iterplay.contract.url import GameUrlParams
from iterplay.errors import ConfigError
from iterplay.evaluation.rubric import load_rubric
from iterplay.evaluation.scorer import DEFAULT_MIN_FPS, Scorer
from iterplay.evaluation.verifier import Verifier, VerifierConfig
from iterplay.harness.browser import BrowserOptions, HarnessBrowser, game_url
from iterplay.harness.playtest import PlaytestConfig, PlaytestRunner
from iterplay.harness.server import game_server
from iterplay.harness.smoke import SmokeConfig, SmokeTester
from iterplay.llm.base import LLMClient
from iterplay.llm.cache import CachingLLMClient, ResponseCache
from iterplay.llm.gemini import GeminiClient
from iterplay.llm.metering import MeteredLLMClient, UsageMeter
from iterplay.media.ffmpeg import FFmpeg
from iterplay.media.prep import MediaPreparer
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItemId
from iterplay.models.run import (
    Budgets,
    CriticBackendKind,
    RunConfig,
    RunIndex,
    RunSummary,
    VersionOutcome,
)
from iterplay.models.verification import VerificationStatus, VerifiedIssue
from iterplay.orchestrator.loop import AgentFactory, Agents, Orchestrator, Stages
from iterplay.orchestrator.policy import KeepBestPolicy, StopPolicy
from iterplay.replay import Replayer, ReplayStep
from iterplay.serving import LOOPBACK_HOST, LoopbackHTTPServer
from iterplay.settings import ENV_PREFIX, Settings, require_api_key
from iterplay.storage.artifacts import read_model
from iterplay.storage.layout import RunPaths, VersionPaths
from iterplay.storage.run_store import RunStore
from iterplay.viewer.server import viewer_server

BOT_SEEDS: Final[Mapping[BotName, int]] = MappingProxyType(
    {BotName.RANDOM: 1, BotName.SPEEDRUNNER: 2, BotName.NEWBIE: 3}
)
"""Each bot's own seed, the same in every run, so versions and runs compare."""

CriticFactory: TypeAlias = Callable[[LLMClient], CriticBackend]
"""Makes a version's critic around that version's cached, metered model client."""

_GAME_PORT_VARIABLE: Final = f"{ENV_PREFIX}GAME_PORT"
_VIEWER_PORT_VARIABLE: Final = f"{ENV_PREFIX}VIEWER_PORT"

_MEDIA_TOOLS: Final = ("ffmpeg", "ffprobe")
"""Programs media prep runs; :class:`~iterplay.media.ffmpeg.FFmpeg` looks them up on PATH."""

_SLUG_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_CHARS: Final = 40

_IDLE_S: Final = 3600.0
"""How long the serving commands sleep at a time while waiting for Ctrl-C."""

_MAX_OBSERVATION_CHARS: Final = 160
"""Longest issue observation ``iterplay replay`` prints."""

_OUTCOMES: Final[Mapping[VersionOutcome, str]] = MappingProxyType(
    {
        VersionOutcome.ACCEPTED: "accepted",
        VersionOutcome.REVERTED: "reverted, because it scored below the best version",
        VersionOutcome.BUILD_FAILED: "build failed: it never booted",
    }
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NewRunOptions:
    design_doc: Path
    rubric: Path
    max_iterations: int
    critic_backend: CriticBackendKind
    headless: bool
    template: TemplateId

    run_id: str | None = None
    """Name of the run directory; generated when None."""

    per_persona: bool = False
    """Critique each bot's clip separately and merge with a Lead Designer (HLD §5)."""


async def new_run(
    options: NewRunOptions,
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    playtest: PlaytestConfig | None = None,
) -> RunSummary:
    """``iterplay new``: create ``runs/<id>/``, wire every stage, and run the loop.

    Wiring: ``TEMPLATES[options.template]`` supplies the prompts (``PromptLibrary``) and the
    control scheme shared by the smoke test, sessions and bots;
    ``CachingLLMClient(MeteredLLMClient(GeminiClient))``, cached in each version's ``llm/``; a
    headless browser for smoke tests and one honouring ``options.headless`` for playtests;
    and the critic backend chosen by ``options.critic_backend``.

    The run id defaults to ``<UTC YYYYmmdd-HHMMSS>-<slug of the design doc's name>``. Every
    input is checked before the run directory is created or any model is called, and
    ``runs/index.json`` is refreshed once the run exists and again when it ends, however it
    ends.

    ``llm`` replaces the Gemini client, so tests need no API key; its calls are still cached
    and metered. ``playtest`` replaces the default playtest configuration, e.g. to shorten
    the runs.

    Raises:
        ConfigError: The API key is missing or an input file is invalid. Also when the run id
            is invalid or taken, the critic backend is unavailable, the game port is in use,
            or ffmpeg is not installed.
    """
    template = TEMPLATES[options.template]
    prompts = PromptLibrary(template)
    design_doc = _read_design_doc(options.design_doc)
    rubric = load_rubric(options.rubric)
    client = llm if llm is not None else GeminiClient(require_api_key(settings), settings.model)
    critic = _critic_factory(
        options.critic_backend, settings, prompts, per_persona=options.per_persona
    )
    config = _run_config(options, settings, design_doc, rubric)
    _require_media_tools()
    _require_free_port(settings.game_port, _GAME_PORT_VARIABLE)
    meter = UsageMeter(config.budgets, config.pricing)
    store = RunStore(settings.runs_dir)
    async with (
        HarnessBrowser(BrowserOptions(headless=True)) as smoke_browser,
        HarnessBrowser(BrowserOptions(headless=options.headless)) as playtest_browser,
    ):
        run = _create_run(store, config, design_doc, settings.runs_dir)
        try:
            _refresh_index(store)
            _log.info("Run %s: writing every artifact under %s", config.run_id, run.root)
            orchestrator = Orchestrator(
                config=config,
                run=run,
                design_doc=design_doc,
                agents=_agent_factory(client, meter, prompts, critic, model_name=config.model),
                stages=_stages(
                    config,
                    template.controls,
                    smoke_browser,
                    playtest_browser,
                    playtest=playtest,
                    game_port=settings.game_port,
                ),
                keep_best=KeepBestPolicy(),
                stop=StopPolicy(target=rubric.target, budgets=config.budgets, meter=meter),
                meter=meter,
            )
            return await orchestrator.run()
        finally:
            _refresh_index(store)


def play(path: Path, settings: Settings, *, seed: int = 1) -> None:
    """``iterplay play``: serve a game on 127.0.0.1 and open it in the default browser.

    ``path`` is a version directory (``runs/<id>/vN``) or a game directory containing
    ``index.html``. The game runs in real time, without ``fixedStep`` or the harness HUD.
    The browser is opened once the server listens, and serving stops at Ctrl-C.

    Raises:
        ConfigError: ``path`` holds no game, or the game port is in use.
    """
    game_dir = _game_dir(path)
    server = game_server(game_dir, settings.game_port)
    with _serving(server, _GAME_PORT_VARIABLE):
        url = _play_url(server.base_url, seed)
        print(f"Playing {game_dir} at {url}")
        print("Press Ctrl-C to stop.")
        webbrowser.open(url)
        _wait_for_interrupt()


def replay(run_dir: Path, *, auto_advance_s: float | None = None) -> None:
    """``iterplay replay``: step through a finished run from disk, without calling the API.

    Prints each version's score, verified issues and patch notes, then waits for Enter, or
    for ``auto_advance_s`` seconds when given. The end of input (Ctrl-D) or Ctrl-C ends the
    replay early.

    Raises:
        ValueError: ``auto_advance_s`` is negative or not finite, or an artifact is invalid.
        RunNotFoundError: ``run_dir`` is not a run with a ``summary.json``, or a version it
            lists has no ``score.json``.
    """
    if auto_advance_s is not None and not (math.isfinite(auto_advance_s) and auto_advance_s >= 0):
        raise ValueError(f"auto_advance_s must be a finite number of seconds, not {auto_advance_s}")
    run = RunStore(run_dir.parent).open(run_dir)
    steps = Replayer(run).steps()
    print(f"Replaying run {run.run_id} from {run.root}")
    shown = 0
    for step in steps:
        if shown and not _advance(step.record.version, auto_advance_s):
            break
        print(f"\n{_describe_step(step)}")
        shown += 1
    print(f"\nEnd of the replay: {_count(shown, 'version')} shown.")


def view(settings: Settings) -> None:
    """``iterplay view``: refresh ``runs/index.json`` and serve the viewer on 127.0.0.1.

    Serving stops at Ctrl-C.

    Raises:
        ConfigError: The viewer port is in use.
    """
    index = _write_index(settings.runs_dir)
    server = viewer_server(settings.runs_dir, settings.viewer_port)
    with _serving(server, _VIEWER_PORT_VARIABLE):
        runs = _count(len(index.run_ids), "run")
        print(f"Viewing {runs} from {settings.runs_dir} at {server.base_url}")
        print("Press Ctrl-C to stop.")
        _wait_for_interrupt()


def _read_design_doc(path: Path) -> str:
    """The design doc's exact text, newlines untouched, so its hash matches the file.

    Raises:
        ConfigError: The file cannot be read, is not UTF-8, or is blank.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read design doc {path}: {exc.strerror or exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"Design doc {path} is not UTF-8 text") from None
    if not text.strip():
        raise ConfigError(f"Design doc {path} is empty")
    return text


def _run_config(
    options: NewRunOptions, settings: Settings, design_doc: str, rubric: Rubric
) -> RunConfig:
    """``config.json`` for a new run.

    Raises:
        ConfigError: The run id or iteration count is invalid.
    """
    created_at = datetime.now(UTC)
    try:
        return RunConfig(
            run_id=options.run_id or _generated_run_id(options.design_doc, created_at),
            created_at=created_at,
            design_doc_sha256=hashlib.sha256(design_doc.encode("utf-8")).hexdigest(),
            template=options.template,
            rubric=rubric,
            model=settings.model,
            bot_seeds=dict(BOT_SEEDS),
            budgets=Budgets(max_iterations=options.max_iterations),
            critic_backend=options.critic_backend,
            headless=options.headless,
        )
    except ValidationError as exc:
        raise ConfigError(f"Cannot start the run: {_describe(exc)}") from None


def _generated_run_id(design_doc: Path, created_at: datetime) -> str:
    """``<UTC YYYYmmdd-HHMMSS>-<slug>``, e.g. ``20261025-143000-tunnel-turbulence``."""
    stamp = created_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    slug = _slug(design_doc.stem)
    return f"{stamp}-{slug}" if slug else stamp


def _slug(text: str) -> str:
    """Lowercase ASCII letters and digits, with ``-`` for everything between them."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_SEPARATORS.sub("-", ascii_text.lower()).strip("-")
    return slug[:_MAX_SLUG_CHARS].rstrip("-")


def _critic_factory(
    kind: CriticBackendKind,
    settings: Settings,
    prompts: PromptLibrary,
    *,
    per_persona: bool = False,
) -> CriticFactory:
    """How each version's critic is made.

    Called before the run starts, so a backend that cannot be built fails before any run
    directory or model call exists. ``per_persona`` selects DirectCalls' persona critics
    and Lead Designer instead of a single critique of the combined clip.

    Raises:
        ConfigError: The backend is not available (``managed``, for now).
    """
    if kind is CriticBackendKind.MANAGED:
        managed = ManagedAgents(settings, prompts)
        return lambda _llm: managed
    return lambda llm: DirectCalls(llm, prompts, per_persona=per_persona)


def _agent_factory(
    llm: LLMClient,
    meter: UsageMeter,
    prompts: PromptLibrary,
    critic: CriticFactory,
    *,
    model_name: str,
) -> AgentFactory:
    """Agents whose model calls are metered against the run's budget and cached in each
    version's ``llm/``, so cache hits cost nothing."""
    metered = MeteredLLMClient(llm, meter)

    def agents(paths: VersionPaths) -> Agents:
        client = CachingLLMClient(metered, ResponseCache(paths.llm_dir, model_name))
        return Agents(
            builder=Builder(client, prompts),
            critic=critic(client),
            fixer=Fixer(client, prompts),
        )

    return agents


def _stages(
    config: RunConfig,
    controls: ControlScheme,
    smoke_browser: HarnessBrowser,
    playtest_browser: HarnessBrowser,
    *,
    playtest: PlaytestConfig | None,
    game_port: int,
) -> Stages:
    """The local stages, driving every game through the template's ``controls``."""
    return Stages(
        smoke=SmokeTester(smoke_browser, controls, SmokeConfig(seed=config.game_seed)),
        playtest=PlaytestRunner(playtest_browser, controls, playtest),
        media=MediaPreparer(FFmpeg()),
        verifier=Verifier(VerifierConfig(min_fps=_min_fps(config.rubric))),
        scorer=Scorer(config.rubric),
        serve_game=lambda game_dir: game_server(game_dir, game_port),
    )


def _min_fps(rubric: Rubric) -> float:
    """The ``performance`` item's ``min_fps``, so the Verifier and Scorer agree on stutter."""
    for item in rubric.items:
        if item.id == RubricItemId.PERFORMANCE:
            return item.params.get("min_fps", DEFAULT_MIN_FPS)
    return DEFAULT_MIN_FPS


def _require_media_tools() -> None:
    """Fail before any model call when media prep could not run.

    Raises:
        ConfigError: ffmpeg or ffprobe is not on PATH.
    """
    missing = [tool for tool in _MEDIA_TOOLS if shutil.which(tool) is None]
    if missing:
        raise ConfigError(
            f"{' and '.join(missing)} not found on PATH. Media prep needs ffmpeg; install it, "
            "e.g. with 'brew install ffmpeg'."
        )


def _require_free_port(port: int, variable: str) -> None:
    """Fail before any model call when the game server could not listen on ``port``.

    The probe binds the way the server does (``SO_REUSEADDR``), then closes at once.

    Raises:
        ConfigError: ``port`` is in use on 127.0.0.1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((LOOPBACK_HOST, port))
        except OSError as exc:
            raise ConfigError(_port_problem(port, variable, exc)) from exc


def _create_run(store: RunStore, config: RunConfig, design_doc: str, runs_dir: Path) -> RunPaths:
    """Raises ConfigError if a run with this id already exists."""
    try:
        return store.create(config, design_doc)
    except FileExistsError:
        raise ConfigError(
            f"A run named {config.run_id!r} already exists in {runs_dir}; choose another run id"
        ) from None


def _refresh_index(store: RunStore) -> None:
    """Rewrite ``runs/index.json`` for the viewer; a failure is only logged."""
    try:
        store.write_index()
    except OSError as exc:
        _log.warning("Could not refresh the runs index: %s", exc)


def _write_index(runs_dir: Path) -> RunIndex:
    """Rewrite ``runs/index.json`` and return what it lists."""
    return read_model(RunStore(runs_dir).write_index(), RunIndex)


def _game_dir(path: Path) -> Path:
    """The game in ``path``: the ``game/`` of a version directory, else ``path`` itself.

    Raises:
        ConfigError: Neither contains an ``index.html``.
    """
    for directory in (path / GAME_DIR, path):
        if (directory / ENTRY_HTML).is_file():
            return directory
    raise ConfigError(
        f"No game in {path}: expected a version directory with {GAME_DIR}/{ENTRY_HTML}, or "
        f"a game directory with {ENTRY_HTML}"
    )


def _play_url(base_url: str, seed: int) -> str:
    """The game page for a human: real-time steps, and no harness HUD."""
    return game_url(base_url, GameUrlParams(seed=seed, fixed_step=False, harness_hud=False))


@contextmanager
def _serving(server: LoopbackHTTPServer, variable: str) -> Iterator[LoopbackHTTPServer]:
    """Serve in the background for the ``with`` block.

    Raises:
        ConfigError: The server's port cannot be bound, e.g. because it is in use.
    """
    try:
        server.start()
    except OSError as exc:
        _, port = server.server_address
        raise ConfigError(_port_problem(port, variable, exc)) from exc
    try:
        yield server
    finally:
        server.stop()


def _port_problem(port: int, variable: str, error: OSError) -> str:
    return (
        f"Cannot listen on {LOOPBACK_HOST}:{port}: {error.strerror or error}. Stop whatever "
        f"uses it, or choose another port with {variable}."
    )


def _wait_for_interrupt() -> None:
    """Block the calling thread until Ctrl-C."""
    with suppress(KeyboardInterrupt):
        while True:
            time.sleep(_IDLE_S)


def _advance(version: int, auto_advance_s: float | None) -> bool:
    """Wait before showing ``version``: for Enter, or for ``auto_advance_s`` seconds.

    False, to stop the replay, at the end of input or on Ctrl-C.
    """
    try:
        if auto_advance_s is None:
            input(f"\nPress Enter for v{version}, or Ctrl-D to stop. ")
        else:
            time.sleep(auto_advance_s)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return True


def _describe_step(step: ReplayStep) -> str:
    """One version of a run: its score and outcome, verified issues and patch notes."""
    record, score = step.record, step.score
    parent = record.parent_version
    origin = "first build" if parent is None else f"fixed from v{parent}"
    issues = [] if step.verified is None else step.verified.issues
    lines = [
        f"v{record.version} ({origin}) scored {score.total:.1%}, against a target of "
        f"{score.target:.0%}: {_OUTCOMES[record.outcome]}.",
        f"  Issues: {_issue_counts(step)}",
        *(f"    {_describe_issue(issue)}" for issue in issues),
        *_patch_notes(step.changelog),
    ]
    if step.videos:
        folder = next(iter(step.videos.values())).parent
        lines.append(f"  Recordings in {folder}: {', '.join(step.videos)}")
    return "\n".join(lines)


def _issue_counts(step: ReplayStep) -> str:
    if step.verified is None:
        return f"none, because v{step.record.version} was not critiqued"
    counts = Counter(issue.status for issue in step.verified.issues)
    return ", ".join(f"{counts[status]} {status.value}" for status in VerificationStatus)


def _describe_issue(verified: VerifiedIssue) -> str:
    """``[status] id (severity, rubric item, bot mm:ss): observation``, on one line."""
    issue = verified.issue
    where = " ".join(part for part in (issue.bot, issue.timestamp) if part)
    context = ", ".join(part for part in (issue.severity.value, issue.rubric_item, where) if part)
    observation = " ".join(issue.observation.split())
    if len(observation) > _MAX_OBSERVATION_CHARS:
        observation = observation[: _MAX_OBSERVATION_CHARS - 3].rstrip() + "..."
    return f"[{verified.status.value}] {issue.id} ({context}): {observation}"


def _patch_notes(changelog: str) -> list[str]:
    lines = [line.rstrip() for line in changelog.splitlines() if line.strip()]
    if not lines:
        return ["  Patch notes: none."]
    return ["  Patch notes:", *(f"    {line}" for line in lines)]


def _describe(error: ValidationError) -> str:
    """Every problem pydantic found, as ``field: message (got value)``."""
    problems = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"])
        value = detail.get("input")
        got = f" (got {value!r})" if isinstance(value, str | int | float) else ""
        problems.append(f"{location}: {detail['msg']}{got}" if location else detail["msg"])
    return "; ".join(problems)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
