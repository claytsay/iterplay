# Iterplay: High-Level Design

> **Iterplay** turns a design doc into a 3D browser game, and then improves it automatically. Gemini 4 Argon writes the game. Bot players play it while being recorded. Argon watches the footage against the design doc and patches the code. The loop repeats until the game meets its rubric.

- **Track:** Problem Statement Two: More Dimensions
- **Target:** one MacBook-class laptop, a 6.5-hour build, and a live 3-minute demo
- **Only cloud dependency:** the Gemini API. Everything else (game server, browsers, recording, scoring, viewer) runs locally.

---

## 1. Goals and non-goals

**Goals**
1. **End-to-end loop:** design doc → v1 game → bots play and are recorded → Argon critiques → Argon patches → v2 … → vN.
2. **Measured improvement:** a rubric score is computed for each version, mostly from objective telemetry. The demo shows the score rising, with v1 and vN side by side.
3. **A critic you can trust:** every claim Argon makes from the video is checked against telemetry before it reaches the fixer.
4. **Runs on one laptop, with a fallback:** every loop is cached to disk so the demo can be replayed if the network or the API misbehaves.

**Non-goals (for today)**
- Arbitrary game genres. We support **one genre template**: a single-level 3D platformer or collect-a-thon.
- Multiplayer, audio, mobile, or asset pipelines. The game uses procedural geometry only (no external art), which avoids any asset-rights issues.
- A polished UI. The viewer is a thin local HTML page; the main feature is the loop. The hackathon bans dashboard-first projects.

---

## 2. Architecture

```
                        ┌──────────────────────────── laptop ─────────────────────────────┐
                        │                                                                 │
 design_doc.md ───────► │  ORCHESTRATOR (Python, asyncio)                                 │
 rubric.yaml            │   │                                                             │
                        │   ├─► [1] Builder ──────────── Gemini API (gemini-4-argon) ───► │──► game files (vN)
                        │   │                                                             │
                        │   ├─► [2] Smoke test (Playwright: loads? harness API? errors?)  │
                        │   │                                                             │
                        │   ├─► [3] Playtest runner                                       │
                        │   │      static server 127.0.0.1:8737  ◄── Chromium x3          │
                        │   │      bots: Random | Speedrunner | Newbie                    │
                        │   │      outputs: .webm video + telemetry.jsonl + console.log   │
                        │   │                                                             │
                        │   ├─► [4] Media prep (ffmpeg: label, timestamp, downscale)      │
                        │   │                                                             │
                        │   ├─► [5] Critic ───────────── Gemini API (video + telemetry) ─►│──► critique.json
                        │   │                                                             │
                        │   ├─► [6] Verifier (local, deterministic: claims vs telemetry)  │
                        │   │                                                             │
                        │   ├─► [7] Scorer (rubric → score.json, keep-best/revert)        │
                        │   │                                                             │
                        │   └─► [8] Fixer ────────────── Gemini API (full code + issues) ►│──► game files (vN+1)
                        │                                                                 │
                        │  runs/<run_id>/vN/...  ◄── everything persisted                 │
                        │  Viewer (static HTML on 127.0.0.1) reads runs/                  │
                        └─────────────────────────────────────────────────────────────────┘
```

**Stack**
| Concern | Choice | Why |
|---|---|---|
| Orchestrator | Python 3.11+, `asyncio` | Quick to write, and has good Playwright and Gemini SDKs |
| LLM | `google-genai` SDK, model `gemini-4-argon` | Required by the judging criteria |
| Browser automation and recording | Playwright for Python (Chromium), `record_video_dir` | Built-in video recording and keyboard input |
| Video post-processing | `ffmpeg` (Homebrew) | Timestamp overlays, labels, downscaling, side-by-side views |
| Game runtime | Three.js (a copy vendored into the repo, MIT license), plain ES modules, no build step | Argon can emit runnable files directly |
| Game server | Python `http.server`, **bound to 127.0.0.1 only** | Local only. Nothing is exposed on the venue Wi-Fi |
| Viewer | One static `viewer.html` that reads `runs/*.json` | Enough to show the demo without becoming a dashboard product |

---

## 3. The game contract (the most important interface)

The loop only works if every generated game can be **driven and observed** by the harness. The Builder prompt makes this contract non-negotiable, and the smoke test enforces it.

### 3.1 File layout produced by Argon
```
game/
  index.html        # loads ./vendor/three.module.js and ./main.js
  main.js           # entry point; may import other local modules
  *.js              # optional extra modules (levels, player, physics, ui)
```
`vendor/three.module.js` is supplied by us and copied into every version. Argon must not use a CDN or any other dependency.

### 3.2 URL parameters
- `?seed=<int>`: all randomness comes from a seeded PRNG
- `?fixedStep=1`: the simulation advances at a fixed 1/60 s per frame, so physics doesn't depend on the frame rate of a headless browser
- `?harness=1`: shows a small HUD overlay (a clock and the bot name) to help the video critic

### 3.3 `window.iterplay` harness API
```js
window.iterplay = {
  version: "1",
  ready: false,                 // set to true once the scene is loaded and playable
  getState() {                  // called by the harness about 10 times per second
    return {
      t: 12.34,                 // seconds of game time
      player: { x, y, z, vx, vy, vz, grounded: true },
      goal:   { x, y, z },
      collectibles: [{ id, x, y, z, collected: false }],
      deaths: 0,
      levelComplete: false,
      fps: 58.7,
      events: []                // since the last call: {type:"death"|"collect"|"respawn"|"complete"|"oob", t, ...}
    };
  },
  reset(seed) {}                // restart the level from scratch
};
```

### 3.4 Controls (fixed so that bots stay generic)
Arrow keys or WASD to move, Space to jump, and R to respawn. Bots send real keyboard events, so they exercise the same input path a human would.

---

## 4. Components

### [1] Builder: design doc to v1
- **Input:** `design_doc.md` (mechanics, level layout intent, "feel" targets such as jump height and coyote time), the game contract (§3), and a style guide (low-poly, no external assets).
- **Output:** the complete `game/` directory. The model returns the files as a structured JSON array of `{path, content}`. Each file must be complete; partial diffs aren't allowed in the MVP because full files are more robust and Argon's long decode makes them affordable.
- **Guardrails:** only allowed paths under `game/` are written, everything else is rejected, and file count and size are capped.

### [2] Smoke test, so a playtest is never wasted on a broken build
Headless Playwright loads `index.html?seed=1&fixedStep=1` and checks:
- `window.iterplay.ready === true` within 10 seconds
- `getState()` returns data that matches the expected schema
- there are no uncaught exceptions or console errors
- after 3 seconds of holding "move forward", the player's position has changed

If any check fails, the error output goes straight back to the Fixer as a "build failure" issue, with up to 2 quick retries before counting it as an iteration.

### [3] Playtest runner
Three bot personas run **in parallel**, each in its own Chromium context with its own recording:

| Bot | Behavior | What it tends to find |
|---|---|---|
| **Random** | Random key presses held for random durations | Crashes, falling through the floor, getting stuck in geometry, out-of-bounds |
| **Speedrunner** | Greedy controller: reads `getState()`, steers toward the nearest uncollected item and then the goal, and jumps when blocked (no forward progress for 0.5 s) or at a gap | Whether the level can be finished, unreachable collectibles, difficulty spikes |
| **Newbie** | Speedrunner with input lag, overshoot, and random pauses | Unforgiving jumps, unclear goals, a frustrating difficulty curve |

- Each run lasts **at most 60 seconds**, or ends earlier when `levelComplete` is true.
- Recordings are 1280×720 `.webm`. Telemetry is written as `telemetry.jsonl` (one `getState()` snapshot per line, 10 per second). Console output is saved too.
- On a laptop, cap concurrency at 3 browsers. Headless Chromium falls back to software WebGL, so the generated scenes must stay low-poly. **If frame rates are bad, run headed** (`headless=False`) to get GPU acceleration.

### [4] Media prep
`ffmpeg` burns in the **bot name and a mm:ss timestamp**, so the critic's timestamps line up with telemetry. It downscales to 640×360 at 10 fps to keep video tokens low, and joins the three runs into one sequential clip (Random, then Speedrunner, then Newbie) with title cards. The result is uploaded with the Gemini Files API.

### [5] Critic: Argon watches the gameplay
- **Input:** the design doc, the rubric, the prepared video, a telemetry summary (see below), and the console errors.
- **Telemetry summary:** a compact, pre-computed digest rather than the raw JSONL. It includes per-bot completion, time to complete, deaths with timestamps and positions, out-of-bounds events, stuck intervals, the minimum and median FPS, and which collectibles were never collected.
- **Output** (validated against a JSON schema):
```json
{
  "issues": [
    {
      "id": "I3",
      "bot": "speedrunner",
      "timestamp": "00:42",
      "rubric_item": "collisions",
      "severity": "high",
      "observation": "Player passes through the second wall and falls below the level.",
      "evidence_type": "visual+telemetry",
      "suspected_cause": "Wall collider not added to colliders[] in level.js",
      "fix_hint": "Register wall meshes with the collision system"
    }
  ],
  "feel_notes": ["Jump arc looks floaty vs design doc 'snappy' target"],
  "rubric_assessment": { "goal_clarity": 2, "feel": 3 }
}
```

### [6] Verifier: the "real verification loop"
This is a deterministic Python step that runs before the fixer sees anything. For each issue it:
- **cross-checks with telemetry.** For example, a "fell through floor at 00:42" claim needs an `oob` event or `y < killPlaneY` within ±2 s. "Level impossible" is disproved if any bot has `levelComplete=true`. A "frame stutter" claim needs a matching FPS dip.
- **sorts the issue into one of three groups:** `confirmed`, `unverifiable` (for example, subjective feel; it is kept but given lower priority), or `contradicted` (dropped and logged, which shows judges that we catch model hallucinations).
- **adds deterministic issues that the critic missed,** such as uncaught JS errors, collectibles that are never reachable, or a bot stuck for more than 5 seconds.

### [7] Scorer and regression guard
The rubric (`rubric.yaml`) is weighted, and most items are **objective**:

| Item | Source | Pass condition |
|---|---|---|
| Boots cleanly | Smoke test | All checks pass |
| No runtime errors | Console | 0 uncaught errors across all runs |
| Completable | Telemetry | The Speedrunner completes the level |
| All collectibles reachable | Telemetry | Every collectible is collected by at least one bot |
| Collision integrity | Telemetry | 0 `oob` events not caused by intentional pits |
| Fair difficulty | Telemetry | The Newbie completes it, or has 3 or fewer deaths in 60 s |
| Performance | Telemetry | Minimum FPS ≥ 30 in headed mode |
| Matches design doc | Critic (verified) | Argon scores 0–5 |
| Game feel | Critic (unverifiable) | Argon scores 0–5, given lower weight |

- `score.json` is written for each version.
- **Keep-best:** if vN+1 scores lower than the best version so far, the Fixer's next attempt starts from the best version, with the failed attempt's issues attached as "what not to do."
- **Stop conditions:** the score reaches the target (for example 90%), we hit `max_iterations` (default 5), or the time or spend budget runs out.

### [8] Fixer: Argon patches the game
- **Input:** all current game files (they fit comfortably in context), the confirmed and deterministic issues ranked by severity, the design doc, the contract, and the history of earlier attempts in this run (a summary of what was tried and how the score changed).
- **Output:** a complete replacement set of `game/` files (full rewrite, same format as the Builder) plus a short `changelog` that ties each change to an issue ID.
- The changelog becomes the **"patch notes"** for each version in the viewer, which shows the loop's story clearly in the demo.

---

## 5. Using parallel agents (for the Managed Agents prize)

The MVP uses plain parallel `asyncio.gather` calls, so it works with no extra dependencies. If time allows, swap in Gemini Managed Agents behind the same interface:

```
         ┌─► Critic agent: Random bot video    ─┐
video ──►├─► Critic agent: Speedrunner video    ├─► Lead Designer agent ──► merged, de-duplicated,
         └─► Critic agent: Newbie video         ┘   (resolves conflicts,     prioritized issue list
                                                     applies rubric)
```

- Each persona critic watches only its own bot's run, which gives deeper and more focused critique.
- A **Lead Designer** agent merges the critiques, removes duplicates, and decides what to fix this iteration (at most about 5 issues, so the Fixer stays focused).
- Implement it behind `class CriticBackend` with two implementations, `DirectCalls` and `ManagedAgents`. The exact Managed Agents API should be confirmed from the DeepMind resources doc provided on the day. Don't make the core loop depend on it.

---

## 6. Data layout on disk

```
runs/<run_id>/
  config.json                 # design doc hash, rubric, model, seeds, budgets
  design_doc.md
  v1/
    game/...                  # exact files Argon produced
    smoke.json
    playtest/
      random.webm  speedrunner.webm  newbie.webm
      random.telemetry.jsonl ...
      console.log
      combined_360p.mp4
    critique.raw.json         # what Argon said
    critique.verified.json    # after the Verifier
    score.json
    changelog.md              # empty for v1
    llm/                      # raw prompts and responses (for cache and replay)
  v2/ ...
  summary.json                # score per version, best version, timings, token usage
```

- **Replay mode:** `iterplay replay <run_id>` steps through a finished run without calling the API. This is the safety net for the demo.
- The `llm/` cache is keyed by a hash of the prompt and the media, so re-running an identical step costs nothing.

---

## 7. CLI

```bash
iterplay new  --doc examples/sky_islands.md --rubric rubric.yaml --iters 5
iterplay play runs/<id>/v3          # open a version in a normal browser to play by hand
iterplay replay runs/<id>           # demo fallback: step through cached iterations
iterplay view                       # serve viewer.html on 127.0.0.1
```

**The viewer** is one HTML page with:
- a version timeline (v1 … vN) with a score sparkline,
- the v1 and current-version videos side by side,
- the verified issues, with each timestamp seeking the video when clicked,
- the patch notes for each version.

---

## 8. Laptop constraints and how we handle them

| Constraint | Mitigation |
|---|---|
| Headless WebGL is slow (software rendering) | Low-poly scenes, a cap of 3 concurrent browsers, `fixedStep=1` so physics stays correct at low FPS, and headed mode when measuring performance |
| CPU load from video encoding | Record at 720p and downscale once afterwards; runs last at most 60 s |
| Venue Wi-Fi is unreliable | Only the Gemini calls use the network. Pre-run full loops before 3 PM, and keep a phone hotspot as a backup |
| API latency during the live demo | Show the cached v1→v5 sequence, then run **one** live iteration on stage |
| API rate limits and spend | Per-run token and spend budget in `config.json`, the response cache, and downscaled video |
| Disk usage | About 30–60 MB per version, which is fine. Add `runs/` to `.gitignore` except for one curated demo run |

---

## 9. Security and hygiene (the repo must be public)

- `GEMINI_API_KEY` is read from an environment variable or a `.env` file that is **listed in `.gitignore` from the very first commit**. Run a quick `git grep -i "api_key\|AIza"` before pushing.
- The game server and viewer bind to **`127.0.0.1` only**, never to `0.0.0.0`. Generated code is untrusted, so it should never be served onto the venue network.
- Generated files are written only under `runs/<id>/vN/game/`, after path checks (no `..`, no absolute paths).
- Generated game code runs only inside the Playwright browser sandbox. It is **never** run with Node or Python.
- Vendored Three.js keeps its MIT license file. All game art is procedural, so there are no asset-rights questions.

---

## 10. Build plan for 10:30 to 5:00 (team of up to 4)

| Time | A: Harness | B: Agents and prompts | C: Bots and telemetry | D: Viewer and demo |
|---|---|---|---|---|
| 10:30–11:30 | Repo skeleton, CLI, local server, Playwright recording working on a hand-written test game | Builder prompt and contract. Get v1 generating from the sample design doc | Harness API spec, Random bot, telemetry recorder | Write 2 sample design docs and `rubric.yaml` |
| 11:30–1:00 | Smoke test, run directory layout, LLM cache | Critic prompt and JSON schema, Files API upload | Speedrunner and Newbie bots, telemetry summary | ffmpeg media prep (labels, timestamps, side-by-side) |
| 1:00–2:30 | **Wire the full loop end to end** (v1 → v2) | Fixer prompt, history, keep-best | Verifier and Scorer | Viewer: timeline, videos, issues |
| 2:30–3:00 | **Milestone: one full 5-iteration run completed and cached** | | | |
| 3:00–4:15 | Hardening, replay mode | Parallel critics and Lead Designer (Managed Agents if possible) | Tuning for flaky bots | Record the 1-minute submission video |
| 4:15–5:00 | Freeze the code. README with a "Built during the hackathon" section. Make the repo public. Submit. | | | |

**Cut order if behind schedule:** Managed Agents → Newbie bot → viewer polish (fall back to opening the videos directly) → side-by-side ffmpeg view. **Never cut:** the Verifier and the Scorer. They are the "real verification loop" the judges score.

---

## 11. Demo script (3 minutes, no slides)

1. **(0:00–0:20) The user and the problem:** "Indie devs can generate a game in one prompt, but the first draft is always broken, and nobody has a QA team."
2. **(0:20–0:50) Live input:** a judge suggests a twist on the sample design doc. Run `iterplay new` and show v1 being generated and the bots starting to play.
3. **(0:50–1:50) The cached loop:** in the viewer, v1's Speedrunner falls through a wall at 00:42. Click Argon's timestamped issue and show that the Verifier confirmed it from telemetry, and that it rejected one hallucinated claim. Step through the patch notes for v2 to v5 while the score climbs from 35% to 92%.
4. **(1:50–2:30) Side by side:** v1 and v5 videos playing together.
5. **(2:30–3:00) A judge plays v5 live** in the browser. Close with the line "One pass is a draft. Iterplay ships the fifth."

---

## 12. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Argon breaks the harness contract (`window.iterplay` missing) | Medium | The smoke test catches it, and the error goes back to the Fixer. The contract is repeated at the end of every Builder and Fixer prompt |
| A fix causes a regression | Medium | Keep-best and revert, plus failed-attempt history passed to the Fixer |
| The critic invents issues | Medium | The Verifier drops contradicted claims, and the viewer shows them as caught hallucinations |
| Bots are too dumb to finish good levels | Medium | The Speedrunner reads goal positions from telemetry rather than "seeing" the screen. Tune it on a hand-written reference level early |
| The loop is too slow live (generation plus 3 runs plus critique) | High | 60-second runs, downscaled video, and a cached replay for most of the demo |
| Headless frame rates are misleading | Medium | Measure performance only in headed mode, and keep `fixedStep` on for correctness checks |

---

## 13. Stretch goals (only after 3 PM)

- An **LLM-driven bot** that uses a fast companion model to choose inputs from screenshots, for playtesting that resembles human play.
- **Several design docs in parallel** (one per Managed Agent), to show the system generalizes.
- A **"human-in-the-loop" note:** the user types "make the jumps snappier" and it is injected as a high-priority issue.
- Exporting the final version as a single `index.html` that can be shared.
