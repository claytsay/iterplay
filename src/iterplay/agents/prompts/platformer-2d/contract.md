## Game contract (non-negotiable)

A test harness drives and observes your game through this contract. A build that breaks any rule fails its smoke test.

### Files
- Return every file in full. Partial files and diffs are rejected.
- Allowed paths: `index.html` and flat ES modules such as `main.js` or `player.js`. Any other path is discarded.
- `index.html` loads the game with `<script type="module" src="./main.js"></script>`.
- Import Three.js only as `import * as THREE from "./vendor/three.module.js";`. It is supplied for you; never write files under `vendor/`.
- No CDNs, network requests or other dependencies. No external assets: all geometry, materials and effects are procedural.

### URL parameters
- `?seed=<int>`: derive all randomness from a PRNG seeded with this value.
- `?fixedStep=1`: advance the simulation exactly 1/60 s per rendered frame, whatever the real frame rate.
- `?harness=1`: show a small HUD with the game clock (mm:ss) and the bot name given by `?bot=<name>`.

### World and controls
- The game is 2D and seen from the side. The level lies in the x/y plane, with x to the right and y up. Gameplay never moves along z; use z only to layer visuals.
- Left/Right arrows or A/D run. Space, Up or W jump. R respawns the player.

### `window.iterplay`
```js
window.iterplay = {
  version: "1",
  ready: false,        // set to true once the scene is loaded and playable
  getState() { ... },  // called about 10 times per second
  reset(seed) { ... }, // restart the level from scratch
};
```

`getState()` returns an object matching this JSON schema. Omit `z` and `vz`, or report them as 0:

$state_schema

- `t` is game time in seconds and `fps` the measured frame rate.
- `events` holds only the events since the previous `getState()` call: `{"type": "death" | "collect" | "respawn" | "complete" | "oob", "t": <game time>}`. `collect` events include the collectible's `id`.
- When the player falls below y = $kill_plane_y, emit an `oob` event and respawn them. Add `"intentional": true` when they fell into a pit the level design intends.
- `getState()` must never throw.
