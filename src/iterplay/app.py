"""The use case behind each CLI command, and the composition root.

This is the only module that chooses concrete implementations (Gemini, Playwright, ffmpeg);
everything else receives its collaborators through constructors.
"""

from dataclasses import dataclass
from pathlib import Path

from iterplay.contract.templates import TemplateId
from iterplay.models.run import CriticBackendKind, RunSummary
from iterplay.settings import Settings


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


async def new_run(options: NewRunOptions, settings: Settings) -> RunSummary:
    """``iterplay new``: create ``runs/<id>/``, wire every stage, and run the loop.

    Wiring: ``TEMPLATES[options.template]`` supplies the prompts (``PromptLibrary``) and the
    control scheme shared by the smoke test, sessions and bots;
    ``CachingLLMClient(MeteredLLMClient(GeminiClient))``, cached in each version's ``llm/``; a
    headless browser for smoke tests and one honouring ``options.headless`` for playtests;
    and the critic backend chosen by ``options.critic_backend``.

    Raises:
        ConfigError: The API key is missing or an input file is invalid.
    """
    raise NotImplementedError


def play(path: Path, settings: Settings, *, seed: int = 1) -> None:
    """``iterplay play``: serve a game on 127.0.0.1 and open it in the default browser.

    ``path`` is a version directory (``runs/<id>/vN``) or a game directory containing
    ``index.html``. The game runs in real time, without ``fixedStep`` or the harness HUD.
    """
    raise NotImplementedError


def replay(run_dir: Path, *, auto_advance_s: float | None = None) -> None:
    """``iterplay replay``: step through a finished run from disk, without calling the API.

    Prints each version's score, verified issues and patch notes, then waits for Enter, or
    for ``auto_advance_s`` seconds when given.
    """
    raise NotImplementedError


def view(settings: Settings) -> None:
    """``iterplay view``: refresh ``runs/index.json`` and serve the viewer on 127.0.0.1."""
    raise NotImplementedError
