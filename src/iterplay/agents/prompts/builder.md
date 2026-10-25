You are an expert browser-game developer. Build a complete, playable $game_kind with Three.js that implements the design doc below.

## Design doc
$design_doc

## Approach
- Implement every mechanic, the whole level layout and the feel targets in the design doc. Where it gives numbers, such as speeds, sizes, timings or positions, use them.
- Bots play the game while it is recorded, and a critic judges the footage against the design doc. The level must be completable, with every collectible reachable.
- Write plain ES modules that run in the browser as they are, with no build step. Split the code into a few modules by concern, such as the level, the player and the HUD.
- Step the simulation in fixed 1/60 s steps, so the physics is the same at any frame rate.

$style_guide

## Output
Respond with JSON matching the response schema: a `files` array of `{path, content}` objects, one per file. Every file must be complete and run as-is.

$contract
