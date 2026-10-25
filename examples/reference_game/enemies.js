// Rock mites patrol the floor; bats swoop back and forth in place. Touching either knocks a
// player back (see game.js). Anything the cave-in reaches is destroyed.
import { TUNING as T } from "./tuning.js";

export const MITE_HALF_WIDTH = 0.4;
export const MITE_HEIGHT = 0.5;
export const BAT_HALF_WIDTH = 0.35;
export const BAT_HALF_HEIGHT = 0.2;

export function createEnemies(level, rng) {
  return {
    mites: level.mites.map((spawn) => ({
      x: spawn.x,
      y: spawn.y, // feet
      homeX: spawn.x,
      dir: rng.next() < 0.5 ? -1 : 1,
      vy: 0,
      alive: true,
    })),
    bats: level.bats.map((spawn) => ({
      x: spawn.x,
      y: spawn.y,
      homeX: spawn.x,
      homeY: spawn.y,
      phase: rng.range(0, Math.PI * 2),
      alive: true,
    })),
  };
}

export function updateEnemies(enemies, level, t, dt, caveInX) {
  for (const mite of enemies.mites) {
    if (mite.alive) updateMite(mite, level, dt, caveInX);
  }
  for (const bat of enemies.bats) {
    if (!bat.alive) continue;
    const angle = T.batSpeed * t + bat.phase;
    bat.x = bat.homeX + T.batSwingX * Math.sin(angle);
    bat.y = bat.homeY + T.batSwingY * Math.sin(2 * angle);
    if (bat.x - BAT_HALF_WIDTH < caveInX) bat.alive = false;
  }
}

/** The first live enemy overlapping `box`, or null. */
export function enemyTouching(enemies, box) {
  for (const mite of enemies.mites) {
    if (mite.alive && overlaps(box, mite.x, MITE_HALF_WIDTH, mite.y, mite.y + MITE_HEIGHT)) {
      return mite;
    }
  }
  for (const bat of enemies.bats) {
    if (bat.alive && overlaps(box, bat.x, BAT_HALF_WIDTH, bat.y - BAT_HALF_HEIGHT, bat.y + BAT_HALF_HEIGHT)) {
      return bat;
    }
  }
  return null;
}

function updateMite(mite, level, dt, caveInX) {
  if (mite.x - MITE_HALF_WIDTH < caveInX || mite.y < -5) {
    mite.alive = false;
    return;
  }
  const column = Math.floor(mite.x);
  if (mite.vy !== 0 || !level.isSolid(column, Math.floor(mite.y - 0.05))) {
    // The floor gave way: fall until something catches it.
    mite.vy = Math.max(mite.vy - T.gravity * dt, -T.maxFallSpeed);
    const y = mite.y + mite.vy * dt;
    if (level.isSolid(column, Math.floor(y))) {
      mite.y = Math.floor(y) + 1;
      mite.vy = 0;
    } else {
      mite.y = y;
    }
    return;
  }
  const x = mite.x + mite.dir * T.miteSpeed * dt;
  const lead = Math.floor(x + mite.dir * MITE_HALF_WIDTH);
  const turn =
    level.isSolid(lead, Math.floor(mite.y + 0.25)) || // wall ahead
    !level.isSolid(lead, Math.floor(mite.y - 0.05)) || // ledge ahead
    Math.abs(x - mite.homeX) > T.mitePatrol;
  if (turn) mite.dir = -mite.dir;
  else mite.x = x;
}

function overlaps(box, x, halfWidth, minY, maxY) {
  return box.maxX > x - halfWidth && box.minX < x + halfWidth && box.maxY > minY && box.minY < maxY;
}
