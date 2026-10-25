# Vendored Three.js

`iterplay.storage.game_files.write_game` copies the Three.js files in this directory into every generated version at `game/vendor/` (HLD §3.1). Games import it as `./vendor/three.module.js`; the model never writes here.

- Release: **three 0.186.1** (npm), MIT.
- `three.module.js` and `three.core.js`: the ES module build from `build/` (`three.module.js` imports `./three.core.js`).
- `LICENSE`: the release's MIT license, which must ship alongside it (HLD §9).

To upgrade, copy the same files from a newer release's npm package and update the version above.
