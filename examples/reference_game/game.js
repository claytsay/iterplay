// The Tunnel Turbulence simulation. Pure logic with no rendering or DOM access: step() advances
// the world by dt seconds, and snapshot() produces the iterplay getState() payload.
import { createEnemies, enemyTouching, updateEnemies } from "./enemies.js";
import { Level, TILE } from "./level.js";
import { createPlayer, movePlayer, placePlayer, playerBox, tilesUnderfoot } from "./player.js";
import { createRng } from "./rng.js";
import { TUNING as T } from "./tuning.js";

export const KILL_PLANE_Y = -20; // iterplay contract: below this the player is out of bounds
export const VIEW_HALF_WIDTH = (T.viewHeight * T.viewAspect) / 2;
export const GEM_RADIUS = 0.35;
const MAX_QUEUED_EVENTS = 500;
const NO_INPUT = Object.freeze({
  left: false,
  right: false,
  jumpHeld: false,
  jumpPressed: false,
  respawnPressed: false,
});

export class Game {
  constructor({ seed = 1, playerCount = 1 } = {}) {
    this.seed = seed;
    this.rng = createRng(seed);
    this.level = new Level();
    this.enemies = createEnemies(this.level, this.rng);
    this.respawnPoints = [this.level.start, ...this.level.lanterns];
    this.players = Array.from({ length: playerCount }, (_, i) =>
      createPlayer(i, { x: this.level.start.x - 0.8 * i, y: this.level.start.y }),
    );
    // The cave-in stops short of the last lantern, so there is always somewhere to respawn.
    const lastPoint = this.respawnPoints[this.respawnPoints.length - 1];
    this.caveIn = { x: T.caveInStart, maxX: lastPoint.x - T.respawnMargin, collapsedColumns: 0 };
    this.rumble = {
      next: this.rng.range(T.rumbleMinInterval, T.rumbleMaxInterval),
      remaining: 0,
      count: 0,
    };
    this.camera = { x: 0 };
    this.t = 0;
    this.complete = false;
    this.winner = null;
    this.restartRequested = false;
    this.events = [];
    this.#updateCamera(1);
  }

  /** Advance by dt seconds. `intents` holds one entry per player (see input.js). */
  step(dt, intents = []) {
    this.t += dt;
    if (this.complete) {
      if (intents.some((intent) => intent?.respawnPressed)) this.restartRequested = true;
      return;
    }
    this.#advanceCaveIn(dt);
    this.#advanceRumble(dt);
    this.level.updateCrumbling(dt, T.crumbleDelay);
    updateEnemies(this.enemies, this.level, this.t, dt, this.caveIn.x);
    for (const player of this.players) {
      this.#updatePlayer(player, intents[player.index] ?? NO_INPUT, dt);
      if (this.complete) break;
    }
    this.#updateCamera(dt);
  }

  /**
   * The iterplay getState() payload; being 2D, it leaves out z. Drains the events queued
   * since the previous call.
   */
  snapshot(fps) {
    const [first] = this.players;
    const state = {
      t: round(this.t),
      player: describe(first),
      goal: { x: this.level.exit.x, y: this.level.exit.y + 1 },
      collectibles: this.level.gems.map((gem) => ({
        id: gem.id,
        x: gem.x,
        y: gem.y,
        collected: gem.collected,
      })),
      deaths: first.deaths,
      levelComplete: this.complete,
      fps: round(fps, 1),
      events: this.events,
    };
    if (this.players.length > 1) state.players = this.players.map(describe);
    this.events = [];
    return state;
  }

  #updatePlayer(player, intent, dt) {
    if (player.dead) {
      player.deadTimer -= dt;
      if (player.deadTimer <= 0) this.#respawn(player);
      return;
    }
    if (intent.respawnPressed) {
      this.#respawn(player);
      return;
    }

    // Below the tunnel there is nothing to steer around: fall straight down to the kill plane.
    movePlayer(player, player.fellAt ? NO_INPUT : intent, this.level, dt);
    if (player.y < 0 && !player.fellAt) {
      player.fellAt = { x: player.x, y: player.y };
      player.vx = 0;
    }
    if (player.grounded) {
      for (const [x, y] of tilesUnderfoot(player)) this.level.stepOn(x, y);
    }
    this.#reachLanterns(player);
    this.#collectGems(player);
    this.#bumpIntoEnemies(player);
    if (this.#atExit(player)) {
      this.#finish(player);
      return;
    }

    if (player.x - T.playerHalfWidth < this.caveIn.x) {
      this.#kill(player, "crushed");
    } else if (player.y < KILL_PLANE_Y) {
      this.#fallOut(player);
    } else if (this.players.length > 1 && player.x < this.camera.x - VIEW_HALF_WIDTH - 0.5) {
      this.#kill(player, "left behind");
    }
  }

  #reachLanterns(player) {
    for (let i = player.lantern + 1; i < this.respawnPoints.length; i++) {
      if (player.x >= this.respawnPoints[i].x) player.lantern = i;
    }
  }

  #collectGems(player) {
    const box = playerBox(player);
    for (const gem of this.level.gems) {
      if (gem.collected || gem.lost || !touchesCircle(box, gem.x, gem.y, GEM_RADIUS)) continue;
      gem.collected = true;
      player.gems += 1;
      this.#emit(player, { type: "collect", id: gem.id });
    }
  }

  #bumpIntoEnemies(player) {
    if (player.invulnerable > 0) return;
    const enemy = enemyTouching(this.enemies, playerBox(player));
    if (!enemy) return;
    // There is no "hit" event in the iterplay contract; the knockback shows up in telemetry.
    const away = Math.sign(player.x - enemy.x) || -player.facing;
    Object.assign(player, {
      vx: away * T.knockbackX,
      vy: T.knockbackY,
      stun: T.stunTime,
      invulnerable: T.invulnerableTime,
      grounded: false,
      jumpCut: true,
    });
  }

  #atExit(player) {
    const box = playerBox(player);
    const { x, y } = this.level.exit;
    return box.maxX > x - 0.5 && box.minX < x + 0.5 && box.maxY > y && box.minY < y + 2;
  }

  #finish(player) {
    this.complete = true;
    this.winner = player.index;
    player.vx = 0;
    this.#emit(player, { type: "complete", gems: player.gems });
  }

  #fallOut(player) {
    // A fall through an open column (a pit or crumbled rock) is part of the level design;
    // a fall where the floor is still intact means the player clipped through rock.
    const where = player.fellAt ?? player;
    const intentional = this.level.floorTop(Math.floor(where.x)) === 0;
    this.#emit(player, { type: "oob", intentional });
    this.#kill(player, "fell", where);
  }

  /** `where` is reported in the death event: for falls, the point the player dropped out. */
  #kill(player, cause, where = player) {
    this.#emit(player, { type: "death", cause, x: round(where.x), y: round(where.y) });
    Object.assign(player, { dead: true, deadTimer: T.respawnDelay, vx: 0, vy: 0 });
    player.deaths += 1;
  }

  #respawn(player) {
    const point = this.#respawnPoint(player);
    placePlayer(player, point);
    player.invulnerable = T.invulnerableTime;
    this.#emit(player, { type: "respawn", x: point.x });
  }

  /**
   * The furthest lantern reached (by anyone, in a race) that is still safely ahead of the
   * cave-in. Failing that, the first solid ground safely ahead of it, so that dying never
   * skips a player further on than the cave-in forces.
   */
  #respawnPoint(player) {
    const safeX = this.caveIn.x + T.respawnMargin;
    const reached = Math.max(player.lantern, ...this.players.map((p) => p.lantern));
    for (let i = reached; i >= 0; i--) {
      const point = this.respawnPoints[i];
      if (point.x >= safeX && this.level.isSolid(Math.floor(point.x), point.y - 1)) return point;
    }
    return this.level.firstFooting(safeX) ?? this.respawnPoints[this.respawnPoints.length - 1];
  }

  #advanceCaveIn(dt) {
    if (this.t < T.caveInDelay) return;
    const caveIn = this.caveIn;
    caveIn.x = Math.min(caveIn.maxX, caveIn.x + T.caveInSpeed * dt);
    const fullyBehind = Math.min(this.level.width, Math.floor(caveIn.x));
    while (caveIn.collapsedColumns < fullyBehind) {
      this.level.collapseColumn(caveIn.collapsedColumns);
      caveIn.collapsedColumns += 1;
    }
    for (const gem of this.level.gems) {
      if (!gem.collected && gem.x + GEM_RADIUS < caveIn.x) gem.lost = true;
    }
  }

  #advanceRumble(dt) {
    const rumble = this.rumble;
    rumble.remaining = Math.max(0, rumble.remaining - dt);
    rumble.next -= dt;
    if (rumble.next > 0) return;
    rumble.next = this.rng.range(T.rumbleMinInterval, T.rumbleMaxInterval);
    rumble.remaining = T.rumbleDuration;
    rumble.count += 1;
    this.#crackFloorAhead();
  }

  /**
   * Crack a few floor tiles ahead of the cave-in. Only floor at least two tiles thick is
   * cracked, away from lanterns and the exit, so crumbling leaves a dent and never a hole.
   */
  #crackFloorAhead() {
    const level = this.level;
    const keepClear = new Set();
    for (const point of [...this.respawnPoints, level.exit]) {
      for (const offset of [-1, 0, 1]) keepClear.add(Math.floor(point.x) + offset);
    }
    const from = Math.max(1, Math.ceil(this.caveIn.x) + 4);
    const to = Math.min(level.width - 2, from + T.rumbleReach);
    const candidates = [];
    for (let x = from; x <= to; x++) {
      const top = level.floorTop(x);
      if (!keepClear.has(x) && top >= 2 && level.tileAt(x, top - 1) === TILE.ROCK) {
        candidates.push(x);
      }
    }
    for (let n = 0; n < T.rumbleCracks && candidates.length > 0; n++) {
      const [x] = candidates.splice(this.rng.int(0, candidates.length), 1);
      level.crack(x, level.floorTop(x) - 1);
    }
  }

  #updateCamera(dt) {
    const alive = this.players.filter((player) => !player.dead);
    const leader = (alive.length > 0 ? alive : this.players).reduce((a, b) => (b.x > a.x ? b : a));
    const lookAhead = this.players.length > 1 ? 1 : T.lookAhead;
    const target = clamp(leader.x + lookAhead, VIEW_HALF_WIDTH, this.level.width - VIEW_HALF_WIDTH);
    this.camera.x += (target - this.camera.x) * Math.min(1, dt * 4);
  }

  #emit(player, { type, ...details }) {
    const event = { type, t: round(this.t), ...details };
    if (this.players.length > 1) event.player = player.index + 1;
    this.events.push(event);
    if (this.events.length > MAX_QUEUED_EVENTS) this.events.shift();
  }
}

function describe(player) {
  return {
    x: round(player.x),
    y: round(player.y),
    vx: round(player.vx),
    vy: round(player.vy),
    grounded: player.grounded,
  };
}

function touchesCircle(box, cx, cy, radius) {
  const dx = cx - clamp(cx, box.minX, box.maxX);
  const dy = cy - clamp(cy, box.minY, box.maxY);
  return dx * dx + dy * dy < radius * radius;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function round(value, digits = 3) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}
