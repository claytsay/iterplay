## Game contract (non-negotiable)

A test harness drives and observes your game through this contract. A build that breaks any rule fails its smoke test.

### Files
- Return every file in full. Partial files and diffs are rejected.
- Allowed paths: `index.html` and flat ES modules such as `main.js` or `player.js` (letters, digits, `_` and `-`). Any other path is discarded. `index.html` and `main.js` are required.
- `index.html` loads the game with `<script type="module" src="./main.js"></script>`. Give it `<link rel="icon" href="data:,">` so the browser never requests a favicon.
- Import Three.js only as `import * as THREE from "./vendor/three.module.js";`. It is supplied for you; never write files under `vendor/`.
- No CDNs, network requests or other dependencies. No external assets: all geometry, materials and effects are procedural.

### Start-up and errors
- Set `window.iterplay.ready` to true within 10 seconds, once the level is loaded and playable. Play starts at once: no title screen, menu or "press a key to start".
- Any uncaught exception or `console.error` call fails the build. Never use `console.error` for a problem the game recovers from.

### URL parameters
- `?seed=<int>`: derive all randomness from a PRNG seeded with this value. Never use `Math.random()`.
- `?fixedStep=1`: advance the simulation exactly 1/60 s per rendered frame, whatever the real frame rate.
- `?harness=1`: show a small HUD in the top-right corner with the game clock (mm:ss) and the bot name given by `?bot=<name>`.

### World and controls
- The game is 2D and seen from the side. The level lies in the x/y plane, with x to the right and y up. Gameplay never moves along z; use z only to layer visuals.
- Left/Right arrows or A/D run. Space, Up or W jump. R respawns the player.
- Bots press real keys. Listen for `keydown` and `keyup` on `window`, match `event.code` (`ArrowLeft`, `ArrowRight`, `KeyA`, `KeyD`, `Space`, `ArrowUp`, `KeyW`, `KeyR`), and call `preventDefault()` for these keys.

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

```json
$state_schema
```

- `t` is game time in seconds and `fps` the measured frame rate.
- `player` is the player's position and velocity in world units, and `goal` is where the level is completed.
- `collectibles` lists every collectible in the level, collected ones included (with `collected: true`). Each keeps the same `id` for the whole run.
- `deaths` counts the player's deaths since the level started.
- `events` holds only the events since the previous `getState()` call: `{"type": "death" | "collect" | "respawn" | "complete" | "oob", "t": <game time>}`. `collect` events include the collectible's `id`.
- When the player falls below y = $kill_plane_y, emit an `oob` event and respawn them. Add `"intentional": true` when they fell into a pit the level design intends.
- When the player reaches the goal, emit `complete` and keep `levelComplete` true from then on.
- `getState()` must never throw.
