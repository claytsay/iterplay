# Tunnel Turbulence

Race through a collapsing cave before the cave-in catches you, dodging rock mites and bats and grabbing gems on the way. One player races the cave-in; two players also race each other.

Reference implementation: `examples/reference_game/`.

## Mechanics
- Side-on 2D platformer. The tunnel is 100 tiles long and 12 tiles tall. Its full height is always on screen, and the camera follows the leading player with a little look-ahead.
- Run and jump, with no double jump. Releasing jump early gives a shorter hop.
- Reaching the exit portal at the far right ends the run. The 10 gems are optional extras.
- **The cave-in** starts 4 s in, 5.5 tiles behind the start, and advances at 1.6 tiles/s. It swallows the tunnel behind it, with any gems and enemies there, and kills any player it touches. It stops 5 tiles short of the last lantern.
- **Cracked rock** (amber) gives way 0.45 s after a player stands on it. Running across is safe; stopping is not.
- **Rumbles** every 7–10 s shake the screen, shower rubble from the roof, and crack up to three floor tiles 4–34 tiles ahead of the cave-in. They only crack floor at least two tiles thick, away from lanterns and the exit, so they leave dents, never holes.
- **Rock mites** patrol the floor. They turn at walls, at ledges, and 3.5 tiles from home.
- **Bats** swoop in a figure-eight around their perch, 2.5 tiles to either side and 0.6 tiles up and down, about once every 5 s.
- **Enemy contact does not kill.** It knocks the player back (7 tiles/s away and up) and stuns them for 0.35 s. They then blink for 1 s, untouchable.
- **Deaths** come from falling out of the tunnel or being caught by the cave-in. After 0.5 s the player respawns at the furthest lantern they reached that is still at least 5 tiles ahead of the cave-in. If none is, they respawn on the first solid ground 5 tiles ahead of it, so dying is never a shortcut. They blink, untouchable, for 1 s. Lanterns light up once passed.
- **Two players** race to the exit, and the first one there wins. A player who drops off the left edge of the screen is left behind and dies. Respawns use the furthest lantern either player has reached.
- After the finish, a respawn key (R, or Enter for player 2) starts a new run.

## Controls
- One player: Left/Right or A/D run. Space, Up or W jump. R respawns at the last lantern reached.
- Two players (`?players=2`): player 1 uses A/D, W or Space, and R; player 2 uses Left/Right, Up and Enter.

## Level layout
Positions are tile columns counted from the left. The floor is 2 tiles thick unless noted.
1. **Start (x 2).** A flat run with two gems on the floor and one a hop up, then a 2-tile pit (x 10–11).
2. **Mite ledge.** Two one-tile steps up (x 16, x 20) to a ledge patrolled by a rock mite (x 20–27), with a gem a jump above it. Drop back down at x 28.
3. **First pit.** A 3-tile pit (x 34–36), a gem just past it, the first lantern (x 41), and a gem a hop up.
4. **Bat chasm.** Two 3-tile pits (x 48–50, x 53–55) separated by a 2-tile pillar one tile higher. One bat swoops over the first pit and another over the landing beyond it. A gem sits on the floor at x 60.
5. **Cracked bridge (x 64–71).** Two 3-tile spans of cracked rock with nothing under them, on either side of a solid pillar holding a gem. If both spans crumble, the gaps are still jumpable.
6. **Low tunnel.** The second lantern (x 74), then 8 tiles of low roof with 4 tiles of headroom (x 78–85). A rock mite patrols the floor and a gem sits a hop up.
7. **Finish.** The last lantern (x 88), two one-tile steps (x 89, x 91) with the last gem on top, then down to the exit portal (x 97).

## Feel targets
- Run speed 7 tiles/s, reached in about 0.1 s. Ground deceleration 55 tiles/s²; air control 35 tiles/s².
- A jump is about 2.6 tiles high, peaks after about 0.37 s, and covers about 5 tiles at full speed (gravity 38 tiles/s², falls capped at 22 tiles/s). Holding jump for about 0.3 s gives nearly the full height.
- Coyote time 0.12 s and jump buffer 0.12 s. Lips up to 0.45 tiles high, such as the edge of a dent, are climbed without jumping.
- A greedy bot finishes in about 30 s with two to four deaths.
- The cave-in only reaches the last lantern after about 58 s, so a steady player is never caught. Stopping or dying is what costs ground.

## Look
- Low-poly blocks seen side-on against a near-black cave. Warm brown rock varies a little from tile to tile. Cracked rock is amber and shakes before it falls.
- The miner wears an orange suit (blue for player 2), a yellow helmet and a lamp. Gems are spinning magenta, cyan and green octahedra.
- Rock mites are red with glowing eyes. Bats are purple and flap.
- Lanterns glow once reached. The exit is a portal pulsing from cyan to violet.
- The cave-in is a near-black wall that sheds rubble. A blinking "CAVE-IN!" warning appears when it is within 6 tiles of a player.
- The HUD shows gems, deaths and the clock. The finish shows "TUNNEL CLEARED", or "PLAYER n WINS" in a race.

## Harness notes
Beyond the iterplay contract for the 2D platformer template:
- `getState()` describes player 1. Two-player games add a `players` array and tag events with `player`.
- Enemy hits have no contract event. They show up in telemetry as knockback.
- Falling into a pit or through crumbled rock is intentional (`"intentional": true` on `oob`). Falling where the floor is intact is a bug.
