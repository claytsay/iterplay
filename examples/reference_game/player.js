// Player movement: running, buffered and coyote-timed jumps, and tile collisions.
import { TUNING as T } from "./tuning.js";

const HALF_W = T.playerHalfWidth;
const HALF_H = T.playerHalfHeight;
const EPS = 1e-4;

export function createPlayer(index, spawn) {
  const player = { index, deaths: 0, gems: 0, lantern: 0 };
  placePlayer(player, spawn);
  return player;
}

/** Put a player on its feet at a spawn cell, ready to play. */
export function placePlayer(player, spawn) {
  Object.assign(player, {
    x: spawn.x,
    y: spawn.y + HALF_H,
    vx: 0,
    vy: 0,
    facing: 1,
    grounded: true,
    coyote: 0,
    jumpBuffer: 0,
    jumpTime: 0,
    jumpCut: true,
    stun: 0,
    invulnerable: 0,
    dead: false,
    deadTimer: 0,
    fellAt: null, // where the player dropped below the tunnel, if they did
  });
}

export function playerBox(player) {
  return {
    minX: player.x - HALF_W,
    maxX: player.x + HALF_W,
    minY: player.y - HALF_H,
    maxY: player.y + HALF_H,
  };
}

/** Tiles directly under the player's feet, as [x, y] pairs. */
export function tilesUnderfoot(player) {
  const y = Math.floor(player.y - HALF_H - 0.02);
  const tiles = [];
  for (let x = Math.floor(player.x - HALF_W + EPS); x <= Math.floor(player.x + HALF_W - EPS); x++) {
    tiles.push([x, y]);
  }
  return tiles;
}

/** Advance one player by dt seconds. `intent` says which controls are held or just pressed. */
export function movePlayer(player, intent, level, dt) {
  player.stun = Math.max(0, player.stun - dt);
  player.invulnerable = Math.max(0, player.invulnerable - dt);
  const inControl = player.stun === 0;

  const direction = inControl ? (intent.right ? 1 : 0) - (intent.left ? 1 : 0) : 0;
  if (direction !== 0) {
    const accel = player.grounded ? T.groundAccel : T.airAccel;
    player.vx = approach(player.vx, direction * T.runSpeed, accel * dt);
    player.facing = direction;
  } else if (inControl) {
    const decel = player.grounded ? T.groundDecel : T.airAccel;
    player.vx = approach(player.vx, 0, decel * dt);
  }

  player.jumpBuffer =
    inControl && intent.jumpPressed ? T.jumpBuffer : Math.max(0, player.jumpBuffer - dt);
  player.coyote = player.grounded ? T.coyoteTime : Math.max(0, player.coyote - dt);
  if (player.jumpBuffer > 0 && player.coyote > 0) {
    Object.assign(player, {
      vy: T.jumpSpeed,
      grounded: false,
      jumpBuffer: 0,
      coyote: 0,
      jumpTime: 0,
      jumpCut: false,
    });
  }
  player.jumpTime += dt;
  if (!player.jumpCut && !intent.jumpHeld && player.jumpTime >= T.minJumpTime) {
    if (player.vy > 0) player.vy *= T.jumpCutFactor;
    player.jumpCut = true;
  }

  player.vy = Math.max(player.vy - T.gravity * dt, -T.maxFallSpeed);
  moveX(player, level, player.vx * dt);
  moveY(player, level, player.vy * dt);
  player.grounded = player.vy <= 0 && overlapsRock(level, player.x, player.y - 0.02);
}

function moveX(player, level, dx) {
  if (dx === 0) return;
  const x = player.x + dx;
  if (!overlapsRock(level, x, player.y)) {
    player.x = x;
    return;
  }
  const column = Math.floor(dx > 0 ? x + HALF_W - EPS : x - HALF_W + EPS);
  if (player.vy <= 0) {
    // Step over low lips, such as the edge of a dent left by crumbled rock.
    const top = highestRockTop(level, column, player.y - HALF_H, player.y + HALF_H);
    if (top - (player.y - HALF_H) <= T.stepUp && !overlapsRock(level, x, top + HALF_H + EPS)) {
      player.x = x;
      player.y = top + HALF_H + EPS;
      return;
    }
  }
  player.x = dx > 0 ? column - HALF_W : column + 1 + HALF_W;
  player.vx = 0;
}

function moveY(player, level, dy) {
  if (dy === 0) return;
  const y = player.y + dy;
  if (!overlapsRock(level, player.x, y)) {
    player.y = y;
    return;
  }
  player.y = dy < 0 ? Math.floor(y - HALF_H + EPS) + 1 + HALF_H : Math.floor(y + HALF_H - EPS) - HALF_H;
  player.vy = 0;
}

/** Does a player-sized box centred at (x, y) overlap any solid tile? */
function overlapsRock(level, x, y) {
  const x0 = Math.floor(x - HALF_W + EPS);
  const x1 = Math.floor(x + HALF_W - EPS);
  const y0 = Math.floor(y - HALF_H + EPS);
  const y1 = Math.floor(y + HALF_H - EPS);
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      if (level.isSolid(tx, ty)) return true;
    }
  }
  return false;
}

function highestRockTop(level, column, bottom, top) {
  for (let ty = Math.floor(top - EPS); ty >= Math.floor(bottom + EPS); ty--) {
    if (level.isSolid(column, ty)) return ty + 1;
  }
  return bottom;
}

function approach(value, target, step) {
  return value < target ? Math.min(value + step, target) : Math.max(value - step, target);
}
