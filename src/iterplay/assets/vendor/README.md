# Vendored Three.js

`iterplay.storage.game_files.write_game` copies the Three.js files in this directory into every generated version at `game/vendor/` (HLD §3.1). Games import it as `./vendor/three.module.js`; the model never writes here.

To vendor a release:

1. Copy `build/three.module.js` from an official Three.js release, plus any build file it imports (recent releases also need `build/three.core.js`).
2. Copy the release's `LICENSE` file alongside it. Three.js is MIT-licensed and the license must ship with it (HLD §9).
3. Record the release number here.

Release: _not yet vendored_
