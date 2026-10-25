// Simple: a tiny 2D platformer that implements iterplay's platformer-2d contract with canvas
// 2D drawing instead of Three.js, so harness tests stay fast. One floor runs from x = -4 to
// x = 16. Gem g1 lies on the way, gem g2 floats high enough to need a jump, and the goal flag
// stands at x = 12. Walking off the left end of the floor drops the player through the kill
// plane: a designed pit, so its oob event is intentional.

const STEP = 1 / 60;
const MAX_STEPS_PER_FRAME = 5;
const KILL_PLANE_Y = -20;

const RUN_SPEED = 6;
const ACCELERATION = 60;
const GRAVITY = 30;
const JUMP_SPEED = 10;
const HALF_WIDTH = 0.4;
const HALF_HEIGHT = 0.5;
const GEM_RADIUS = 0.4;

const FLOOR = { left: -4, right: 16, bottom: -2, top: 0 };
const START = { x: 0, y: FLOOR.top + HALF_HEIGHT };
const GOAL = { x: 12, y: FLOOR.top + HALF_HEIGHT };
const GOAL_AREA = { left: GOAL.x - 0.5, right: GOAL.x + 0.5, bottom: FLOOR.top, top: 2 };
const GEMS = [
  { id: "g1", x: 3, y: 0.5 },
  { id: "g2", x: 7, y: 2.2 },
];

const KEYS = {
  left: ["ArrowLeft", "KeyA"],
  right: ["ArrowRight", "KeyD"],
  jump: ["Space", "ArrowUp", "KeyW"],
  respawn: ["KeyR"],
};

const params = new URLSearchParams(window.location.search);
const options = {
  seed: parseSeed(params.get("seed"), 1),
  fixedStep: params.get("fixedStep") === "1",
  harness: params.get("harness") === "1",
  bot: params.get("bot") || "human",
};

const held = new Set();
const pressed = new Set();
const handled = new Set(Object.values(KEYS).flat());
window.addEventListener("keydown", (event) => {
  if (!handled.has(event.code)) return;
  event.preventDefault();
  if (!event.repeat) pressed.add(event.code);
  held.add(event.code);
});
window.addEventListener("keyup", (event) => held.delete(event.code));
window.addEventListener("blur", () => held.clear());

const canvas = document.getElementById("stage");
const ctx = canvas.getContext("2d");
const hud = document.getElementById("hud");
hud.hidden = !options.harness;
const fps = { value: 0, frames: 0, since: performance.now() };
let game = createGame(options.seed);
let accumulator = 0;
let previous = performance.now();

window.iterplay = {
  version: "1",
  ready: false,
  getState() {
    const { player } = game;
    const state = {
      t: round(game.t),
      player: {
        x: round(player.x),
        y: round(player.y),
        vx: round(player.vx),
        vy: round(player.vy),
        grounded: player.grounded,
      },
      goal: { x: GOAL.x, y: GOAL.y },
      collectibles: game.gems.map(({ id, x, y, collected }) => ({ id, x, y, collected })),
      deaths: game.deaths,
      levelComplete: game.complete,
      fps: round(fps.value, 1),
      events: game.events,
    };
    game.events = [];
    return state;
  },
  reset(seed) {
    options.seed = parseSeed(seed, options.seed);
    game = createGame(options.seed);
  },
};

function createGame(seed) {
  const random = createRng(seed);
  return {
    t: 0,
    player: spawnPlayer(),
    gems: GEMS.map((gem) => ({ ...gem, collected: false })),
    deaths: 0,
    complete: false,
    events: [],
    stars: Array.from({ length: 40 }, () => ({ x: random() * 26 - 6, y: 1 + random() * 8 })),
  };
}

function spawnPlayer() {
  return { x: START.x, y: START.y, vx: 0, vy: 0, grounded: true };
}

function sampleInput() {
  const any = (codes, keys) => codes.some((code) => keys.has(code));
  const input = {
    left: any(KEYS.left, held),
    right: any(KEYS.right, held),
    jump: any(KEYS.jump, pressed),
    respawn: any(KEYS.respawn, pressed),
  };
  pressed.clear();
  return input;
}

function step(input) {
  game.t += STEP;
  if (game.complete) return;
  if (input.respawn) {
    respawn();
    return;
  }
  const player = game.player;
  const direction = (input.right ? 1 : 0) - (input.left ? 1 : 0);
  player.vx = approach(player.vx, direction * RUN_SPEED, ACCELERATION * STEP);
  if (input.jump && player.grounded) player.vy = JUMP_SPEED;
  player.vy -= GRAVITY * STEP;
  moveX(player, player.vx * STEP);
  moveY(player, player.vy * STEP);
  for (const gem of game.gems) {
    if (!gem.collected && touchesCircle(box(player), gem, GEM_RADIUS)) {
      gem.collected = true;
      emit({ type: "collect", id: gem.id });
    }
  }
  if (overlaps(box(player), GOAL_AREA)) {
    game.complete = true;
    Object.assign(player, { vx: 0, vy: 0 });
    emit({ type: "complete" });
  } else if (player.y < KILL_PLANE_Y) {
    emit({ type: "oob", intentional: true });
    emit({ type: "death" });
    game.deaths += 1;
    respawn();
  }
}

function moveX(player, dx) {
  player.x += dx;
  if (dx !== 0 && overlaps(box(player), FLOOR)) {
    player.x = dx > 0 ? FLOOR.left - HALF_WIDTH : FLOOR.right + HALF_WIDTH;
    player.vx = 0;
  }
}

function moveY(player, dy) {
  player.y += dy;
  player.grounded = false;
  if (overlaps(box(player), FLOOR)) {
    player.grounded = dy < 0;
    player.y = dy < 0 ? FLOOR.top + HALF_HEIGHT : FLOOR.bottom - HALF_HEIGHT;
    player.vy = 0;
  }
}

function respawn() {
  game.player = spawnPlayer();
  emit({ type: "respawn" });
}

function emit(event) {
  game.events.push({ ...event, t: round(game.t) });
  if (game.events.length > 500) game.events.shift();
}

function draw() {
  const { width, height } = canvas;
  const cameraX = Math.min(Math.max(game.player.x + 2, 4), 10);
  const scale = 64;
  const sx = (x) => width / 2 + (x - cameraX) * scale;
  const sy = (y) => height * 0.7 - y * scale;
  const fill = (color, area) => {
    ctx.fillStyle = color;
    ctx.fillRect(sx(area.left), sy(area.top), (area.right - area.left) * scale,
      (area.top - area.bottom) * scale);
  };
  ctx.fillStyle = "#1d2b53";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#c2c3c7";
  for (const star of game.stars) ctx.fillRect(sx(star.x), sy(star.y), 3, 3);
  fill("#7e5539", FLOOR);
  fill("#5f574f", { left: GOAL.x - 0.08, right: GOAL.x + 0.08, bottom: 0, top: 2 });
  fill(game.complete ? "#ffec27" : "#00e436", { left: GOAL.x + 0.08, right: GOAL.x + 0.9,
    bottom: 1.4, top: 2 });
  ctx.fillStyle = "#ffa300";
  for (const gem of game.gems) {
    if (gem.collected) continue;
    ctx.beginPath();
    ctx.arc(sx(gem.x), sy(gem.y), GEM_RADIUS * scale, 0, Math.PI * 2);
    ctx.fill();
  }
  fill("#ff004d", box(game.player));
}

function frame(now) {
  const elapsed = Math.min(Math.max(0, (now - previous) / 1000), 0.25);
  previous = now;
  measureFps(now);
  if (options.fixedStep) {
    step(sampleInput());
  } else {
    accumulator += elapsed;
    for (let steps = 0; accumulator >= STEP && steps < MAX_STEPS_PER_FRAME; steps++) {
      step(sampleInput());
      accumulator -= STEP;
    }
    accumulator = Math.min(accumulator, STEP);
  }
  draw();
  hud.textContent = `${clock(game.t)}  ${options.bot}`;
  window.iterplay.ready = true;
  window.requestAnimationFrame(frame);
}

window.requestAnimationFrame(frame);

function measureFps(now) {
  fps.frames += 1;
  const span = (now - fps.since) / 1000;
  if (span >= 0.5) {
    fps.value = fps.frames / span;
    fps.frames = 0;
    fps.since = now;
  }
}

function box(player) {
  return {
    left: player.x - HALF_WIDTH,
    right: player.x + HALF_WIDTH,
    bottom: player.y - HALF_HEIGHT,
    top: player.y + HALF_HEIGHT,
  };
}

function overlaps(a, b) {
  return a.left < b.right && a.right > b.left && a.bottom < b.top && a.top > b.bottom;
}

function touchesCircle(area, { x, y }, radius) {
  const dx = x - Math.min(Math.max(x, area.left), area.right);
  const dy = y - Math.min(Math.max(y, area.bottom), area.top);
  return dx * dx + dy * dy < radius * radius;
}

function approach(value, target, change) {
  return value < target ? Math.min(value + change, target) : Math.max(value - change, target);
}

// Seeded PRNG (mulberry32): the star field is the game's only randomness.
function createRng(seed) {
  let state = seed | 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function clock(seconds) {
  const whole = Math.floor(seconds);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(whole / 60))}:${pad(whole % 60)}`;
}

function round(value, digits = 3) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

function parseSeed(value, fallback) {
  const seed = Number.parseInt(value, 10);
  return Number.isFinite(seed) ? seed : fallback;
}
