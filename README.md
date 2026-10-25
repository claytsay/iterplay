# iterplay

Iterplay turns a design doc into a browser game (for now, a single-level 2D platformer), then improves it automatically. Gemini writes the game, bots play it while being recorded, Gemini critiques the footage against the design doc, a deterministic verifier checks every claim against telemetry, and the fixer patches the code. The loop repeats until the game meets its rubric.

Design: [docs/iterplay_high_level_design.md](docs/iterplay_high_level_design.md). Code comments cite it as "HLD", e.g. `HLD §4 [6]`.

> **Status: skeleton.** Every component, data contract and CLI command in the HLD exists with typed signatures and docstrings. Behaviour is stubbed with `NotImplementedError`.

## Layout

```
src/iterplay/
  contract/      §3     game contract: getState() schema, game templates, controls, URL params,
                        allowed files
  models/        §4 §6  pydantic models for every artifact passed between stages or saved to runs/
  agents/        §4 §5  Builder [1], critic backends [5], Fixer [8], guardrails, prompt templates
  llm/           §6 §8  LLMClient protocol, Gemini adapter, response cache, budget metering
  harness/       §4     Playwright sessions, smoke test [2], playtest runner [3], game server
  bots/          §4     Random, Speedrunner and Newbie personas
  media/         §4     ffmpeg media prep [4]
  evaluation/    §4     telemetry summary, Verifier [6], Scorer [7], rubric loading
  orchestrator/  §2 §4  the iteration loop, keep-best and stop policies
  storage/       §6     run directory layout and artifact I/O
  viewer/        §7     static viewer page and its server
  serving.py     §9     loopback-only static file server
  replay.py      §6     replay mode
  app.py         §7     the use case behind each command; the only place concrete classes are wired
  cli.py         §7     the iterplay command
examples/               Tunnel Turbulence: its design doc (tunnel_turbulence.md) and a hand-written
                        2D implementation of the contract (reference_game/)
rubric.yaml             default scoring rubric
```

## Game templates

The HLD plans a 3D platformer. The current target is a single-level 2D platformer, and 3D is designed in but not built. Everything that depends on the kind of game sits behind a game template ([contract/templates.py](src/iterplay/contract/templates.py)), chosen with `iterplay new --template` and recorded in `config.json`:

- its control scheme: key bindings, the world directions bots steer by, and the control the smoke test holds;
- its contract and style-guide prompts, in `src/iterplay/agents/prompts/<template id>/`;
- how prompts name the game.

The rest of the pipeline is the same for every template. `getState()` positions are 3D everywhere, and 2D games omit z or report it as 0.

The only template is `platformer-2d`. Its contract differs from HLD §3:
- the level lies in the x/y plane (x right, y up);
- Left/Right or A/D run, Space, Up or W jump, and R respawns;
- the smoke test holds Right instead of "move forward".

Adding a 3D template takes a control scheme with forward and back, its two prompts, an entry in `TEMPLATES`, and a reference level to tune the bots on. `tests/test_templates.py` checks that every template is complete.

## Setup

Requires Python 3.11+ and ffmpeg (`brew install ffmpeg`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
cp .env.example .env   # then set GEMINI_API_KEY; .env is git-ignored
```

Then vendor Three.js as described in [src/iterplay/assets/vendor/README.md](src/iterplay/assets/vendor/README.md).

## Usage

```bash
iterplay new --doc examples/tunnel_turbulence.md --rubric rubric.yaml --iters 5
iterplay play runs/<id>/v3   # play a version by hand
iterplay replay runs/<id>    # step through a cached run without calling the API
iterplay view                # serve the viewer on 127.0.0.1
```

## Development

```bash
pytest
ruff check . && ruff format --check .
mypy
```

## Conventions

- **Contracts are models.** Data that crosses a stage boundary or lands on disk is a pydantic model in `iterplay.models`; game-facing schemas live in `iterplay.contract`. Use `StrictModel` for data iterplay produces and `TolerantModel` for data from the model or a generated game.
- **In-memory inputs** such as requests and configs are frozen dataclasses, defined next to the code that consumes them.
- **Dependency injection.** Stages receive collaborators through their constructors, and only `iterplay.app` picks concrete implementations. Bots are pure policies over `GameState`, so they can be tested without a browser.
- **Local only.** Servers bind to 127.0.0.1 and the host is not configurable. The Gemini API is the only network dependency.
- **Generated code is untrusted.** It is written only under `runs/<id>/vN/game/`, after the guardrails, and only ever runs inside the Playwright browser.
- **Secrets** come from the environment or `.env`. Run `git grep -i "api_key\|AIza"` before pushing.

## Contract details beyond the HLD

The skeleton pins down a few details the HLD leaves open. The Builder and Fixer prompts state them to the model.

- Telemetry lines are `{"video_t": ..., "state": <getState() snapshot>}`. `video_t` is seconds since that bot's recording started, so it lines up with the burned-in mm:ss even when `fixedStep` makes game time run slower than real time.
- The smoke test's movement check passes if any `getState()` sample during the hold differs from the first. A player who falls, dies and respawns at the start still counts as moving.
- The kill plane is at y = -20. `oob` events carry `"intentional": true` for falls into designed pits, which the collision-integrity item ignores.
- `?bot=<name>` supplies the bot name the harness HUD shows.
- Generated files are limited to `index.html` and flat `*.js` modules. Three.js is imported as `./vendor/three.module.js`.
- The viewer listens on port 8738, next to the game server on 8737.
