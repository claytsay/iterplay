// Seeded PRNG (mulberry32). All randomness in the game, visual effects included, flows through
// one of these, so a seed always replays the same run.
export function createRng(seed) {
  let state = seed | 0;
  const next = () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return {
    next,
    range: (min, max) => min + (max - min) * next(),
    int: (min, maxExclusive) => min + Math.floor((maxExclusive - min) * next()),
  };
}
