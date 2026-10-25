// Tunnel Turbulence: race through a collapsing cave, dodging rock mites and bats and grabbing
// gems, before the cave-in catches you. One or two players (?players=2). Design doc:
// examples/tunnel_turbulence.md.
//
// This is iterplay's hand-written reference level, for building the harness and tuning bots
// before any generated game exists (HLD §10). It implements the window.iterplay contract for
// the 2D platformer template, as generated games must (src/iterplay/agents/prompts/
// platformer-2d/contract.md). Beyond the contract:
//   - getState() describes player 1; two-player games add a `players` array and tag events
//     with `player`.
//   - Enemies knock players back rather than killing them, so the level stays finishable for
//     bots that cannot see them in getState().
//
// To run it, copy the vendored Three.js files into ./vendor/, then serve this directory on
// loopback and open http://127.0.0.1:8737/ (add ?players=2 for a two-player race):
//   iterplay play examples/reference_game
//   python3 -m http.server 8737 --bind 127.0.0.1 --directory examples/reference_game
import { Game } from "./game.js";
import { Hud } from "./hud.js";
import { Keyboard } from "./input.js";
import { View } from "./view.js";

const STEP = 1 / 60;
const MAX_STEPS_PER_FRAME = 5;

const params = new URLSearchParams(window.location.search);
const options = {
  seed: parseSeed(params.get("seed"), 1),
  fixedStep: params.get("fixedStep") === "1",
  harness: params.get("harness") === "1",
  bot: params.get("bot") ?? "",
  playerCount: params.get("players") === "2" ? 2 : 1,
};

const keyboard = new Keyboard(window, options.playerCount);
const view = new View(document.getElementById("stage"));
const hud = new Hud(document.getElementById("hud"), options);
const fps = createFpsMeter();
let game = startGame(options.seed);
let lastState = null;
let accumulator = 0;

window.iterplay = {
  version: "1",
  ready: false,

  getState() {
    // The contract forbids throwing: report the failure and repeat the last good state.
    try {
      lastState = game.snapshot(fps.value);
    } catch (error) {
      console.error("getState() failed", error);
      lastState = lastState && { ...lastState, events: [] };
    }
    return lastState;
  },

  reset(seed) {
    options.seed = parseSeed(seed, options.seed);
    game = startGame(options.seed);
  },
};

function startGame(seed) {
  const next = new Game({ seed, playerCount: options.playerCount });
  view.load(next);
  return next;
}

function frame(now) {
  fps.tick(now);
  const before = game.t;
  if (options.fixedStep) {
    game.step(STEP, keyboard.sample());
  } else {
    // Fixed-size steps keep the physics identical at any frame rate.
    accumulator += Math.min(fps.elapsed, 0.25);
    for (let steps = 0; accumulator >= STEP && steps < MAX_STEPS_PER_FRAME; steps++) {
      game.step(STEP, keyboard.sample());
      accumulator -= STEP;
    }
    accumulator = Math.min(accumulator, STEP);
  }
  const dt = game.t - before;
  if (game.restartRequested) game = startGame(options.seed);

  view.render(game, dt);
  hud.update(game);
  window.iterplay.ready = true;
  requestAnimationFrame(frame);
}

requestAnimationFrame(frame);

function createFpsMeter() {
  let previous = performance.now();
  let windowStart = previous;
  let frames = 0;
  return {
    value: 0,
    elapsed: 0,
    tick(now) {
      this.elapsed = Math.max(0, (now - previous) / 1000);
      previous = now;
      frames += 1;
      const span = (now - windowStart) / 1000;
      if (span >= 0.5) {
        this.value = frames / span;
        frames = 0;
        windowStart = now;
      } else if (this.value === 0 && this.elapsed > 0) {
        this.value = Math.min(1 / this.elapsed, 240);
      }
    },
  };
}

function parseSeed(value, fallback) {
  const seed = Number.parseInt(value, 10);
  return Number.isFinite(seed) ? seed : fallback;
}
