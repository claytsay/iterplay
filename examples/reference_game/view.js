// Three.js rendering: an orthographic side view of the tunnel. The view only reads Game state.
// Its own seeded RNG drives purely visual effects such as rubble and camera shake.
import * as THREE from "./vendor/three.module.js";
import { BAT_HALF_HEIGHT, BAT_HALF_WIDTH, MITE_HALF_WIDTH, MITE_HEIGHT } from "./enemies.js";
import { TILE } from "./level.js";
import { createRng } from "./rng.js";
import { TUNING as T } from "./tuning.js";

const COLOR = {
  cave: 0x140e0a,
  rock: 0x6b5340,
  cracked: 0xc48a3a,
  rubble: 0x4d3b2d,
  caveIn: 0x070504,
  players: [0xf4a259, 0x5bc0eb],
  helmet: 0xffd166,
  lamp: 0xfff3b0,
  eye: 0x1b1b1b,
  gems: [0xff5dd8, 0x4ff0ff, 0x8cff6b],
  mite: 0xc0392b,
  bat: 0x8e5cc2,
  pole: 0x5c4a3a,
  lanternOff: 0x4a3f33,
  lanternOn: 0xffd166,
  portalFrame: 0x3d5a80,
  portal: [0x7af5ff, 0xc77dff],
};
const SPRITE_Z = 1; // players, enemies and gems sit in front of the rock
const CAVE_IN_Z = 2; // the cave-in covers everything behind it
const MAX_RUBBLE = 64;
const SHAKE = 0.12;

export class View {
  #container;
  #renderer;
  #camera;
  #scene;
  #assets;
  #world = null;
  #parts = [];
  #rng = createRng(0);

  constructor(container) {
    this.#container = container;
    this.#renderer = new THREE.WebGLRenderer({ antialias: false });
    this.#renderer.setPixelRatio(1);
    container.appendChild(this.#renderer.domElement);
    this.#camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 100);
    this.#scene = new THREE.Scene();
    this.#scene.background = new THREE.Color(COLOR.cave);
    this.#assets = createAssets();
    this.resize();
    window.addEventListener("resize", () => this.resize());
  }

  resize() {
    const halfHeight = T.viewHeight / 2;
    const halfWidth = halfHeight * T.viewAspect;
    Object.assign(this.#camera, {
      left: -halfWidth,
      right: halfWidth,
      top: halfHeight,
      bottom: -halfHeight,
    });
    this.#camera.updateProjectionMatrix();
    this.#renderer.setSize(this.#container.clientWidth, this.#container.clientHeight, false);
  }

  /** Build the scene for a new game: on start, on restart and after window.iterplay.reset(). */
  load(game) {
    this.#unload();
    this.#rng = createRng(game.seed ^ 0x5eed);
    const rubble = createRubble(this.#assets, this.#rng);
    this.#parts = [
      createTiles(game.level, this.#assets, this.#rng),
      createLanterns(game.level.lanterns, this.#assets),
      createPortal(game.level.exit, this.#assets),
      createGems(game.level.gems, this.#assets),
      createEnemySprites(game.enemies, this.#assets),
      createPlayerSprites(game.players, this.#assets),
      createCaveInEffects(game, this.#assets, this.#rng, rubble),
      rubble, // last, so it moves the rubble spawned this frame
    ];
    this.#world = new THREE.Group();
    for (const part of this.#parts) this.#world.add(part.object);
    this.#scene.add(this.#world);
  }

  /** Draw a frame. `dt` is the game time that passed since the previous frame. */
  render(game, dt) {
    for (const part of this.#parts) part.update(game, dt);
    const shake = game.rumble.remaining > 0 ? SHAKE : 0;
    this.#camera.position.set(
      game.camera.x + this.#rng.range(-shake, shake),
      T.viewHeight / 2 + this.#rng.range(-shake, shake),
      20,
    );
    this.#renderer.render(this.#scene, this.#camera);
  }

  #unload() {
    for (const part of this.#parts) part.dispose?.();
    this.#world?.removeFromParent();
    this.#world = null;
    this.#parts = [];
  }
}

function createAssets() {
  const material = (color, options = {}) => new THREE.MeshBasicMaterial({ color, ...options });
  return {
    box: new THREE.BoxGeometry(1, 1, 1),
    tile: new THREE.BoxGeometry(0.96, 0.96, 1),
    rubble: new THREE.BoxGeometry(0.22, 0.22, 0.22),
    gem: new THREE.OctahedronGeometry(0.3),
    plane: new THREE.PlaneGeometry(1, 1),
    white: material(0xffffff), // tinted per instance
    players: COLOR.players.map((color) => material(color)),
    helmet: material(COLOR.helmet),
    lamp: material(COLOR.lamp),
    eye: material(COLOR.eye),
    gems: COLOR.gems.map((color) => material(color)),
    mite: material(COLOR.mite),
    bat: material(COLOR.bat),
    pole: material(COLOR.pole),
    lanternOff: material(COLOR.lanternOff),
    lanternOn: material(COLOR.lanternOn),
    portalFrame: material(COLOR.portalFrame),
    caveIn: material(COLOR.caveIn, { transparent: true, opacity: 0.92 }),
  };
}

/** A box of the given size, built from the shared unit cube. */
function block(assets, material, width, height, depth = 0.5) {
  const mesh = new THREE.Mesh(assets.box, material);
  mesh.scale.set(width, height, depth);
  return mesh;
}

function at(object, x, y, z = 0.05) {
  object.position.set(x, y, z);
  return object;
}

/** Every rock tile as one instanced mesh. Cracked tiles change colour, then shake and fall. */
function createTiles(level, assets, rng) {
  const solid = [];
  level.tiles.forEach((tile, i) => tile !== TILE.EMPTY && solid.push(i));
  const mesh = new THREE.InstancedMesh(assets.tile, assets.white, solid.length);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false; // instances shake and fall outside the initial bounds
  const instanceOf = new Int32Array(level.tiles.length).fill(-1);
  const shades = solid.map(() => rng.range(-0.05, 0.05));
  const previous = level.tiles.slice();
  const falling = [];
  const dummy = new THREE.Object3D();
  const color = new THREE.Color();

  const centre = (i) => [(i % level.width) + 0.5, Math.floor(i / level.width) + 0.5];
  const place = (instance, x, y, angle = 0, scale = 1) => {
    dummy.position.set(x, y, 0);
    dummy.rotation.set(0, 0, angle);
    dummy.scale.setScalar(scale);
    dummy.updateMatrix();
    mesh.setMatrixAt(instance, dummy.matrix);
  };
  const paint = (instance, tile) => {
    color.setHex(tile === TILE.CRACKED ? COLOR.cracked : COLOR.rock).offsetHSL(0, 0, shades[instance]);
    mesh.setColorAt(instance, color);
  };

  solid.forEach((i, instance) => {
    instanceOf[i] = instance;
    place(instance, ...centre(i));
    paint(instance, level.tiles[i]);
  });

  return {
    object: mesh,
    update(game, dt) {
      let recoloured = false;
      for (let i = 0; i < level.tiles.length; i++) {
        const tile = level.tiles[i];
        if (tile === previous[i] || instanceOf[i] < 0) continue;
        previous[i] = tile;
        if (tile === TILE.CRACKED) {
          paint(instanceOf[i], tile);
          recoloured = true;
        } else if (tile === TILE.EMPTY) {
          const [x, y] = centre(i);
          const spin = rng.range(-5, 5);
          falling.push({ instance: instanceOf[i], x, y, vx: rng.range(-0.8, 0.8), vy: rng.range(0, 2), spin, angle: 0 });
        }
      }
      for (const [i, elapsed] of level.crumbling) {
        const [x, y] = centre(i);
        place(instanceOf[i], x + 0.06 * Math.sin(elapsed * 70), y);
      }
      for (let k = falling.length - 1; k >= 0; k--) {
        const piece = falling[k];
        piece.vy -= T.gravity * dt;
        piece.x += piece.vx * dt;
        piece.y += piece.vy * dt;
        piece.angle += piece.spin * dt;
        const gone = piece.y < -2;
        place(piece.instance, piece.x, piece.y, piece.angle, gone ? 0 : 1);
        if (gone) falling.splice(k, 1);
      }
      if (level.crumbling.size > 0 || falling.length > 0) mesh.instanceMatrix.needsUpdate = true;
      if (recoloured) mesh.instanceColor.needsUpdate = true;
    },
    dispose: () => mesh.dispose(),
  };
}

function createLanterns(lanterns, assets) {
  const group = new THREE.Group();
  const lamps = lanterns.map((lantern) => {
    group.add(at(block(assets, assets.pole, 0.08, 1), lantern.x, lantern.y + 0.5, 0));
    const lamp = at(block(assets, assets.lanternOff, 0.3, 0.3), lantern.x, lantern.y + 1.1, 0);
    group.add(lamp);
    return lamp;
  });
  return {
    object: group,
    update(game) {
      // Respawn point 0 is the start, so lantern i is respawn point i + 1.
      const reached = Math.max(...game.players.map((player) => player.lantern));
      lamps.forEach((lamp, i) => {
        lamp.material = reached > i ? assets.lanternOn : assets.lanternOff;
      });
    },
  };
}

function createPortal(exit, assets) {
  const group = at(new THREE.Group(), exit.x, exit.y + 1, 0);
  const [from, to] = COLOR.portal.map((hex) => new THREE.Color(hex));
  const glow = new THREE.MeshBasicMaterial({ color: from });
  const inner = new THREE.Mesh(assets.plane, glow);
  inner.scale.set(0.9, 1.9, 1);
  group.add(
    inner,
    at(block(assets, assets.portalFrame, 0.14, 2.1), -0.55, 0, 0.1),
    at(block(assets, assets.portalFrame, 0.14, 2.1), 0.55, 0, 0.1),
    at(block(assets, assets.portalFrame, 1.24, 0.14), 0, 1.05, 0.1),
  );
  return {
    object: group,
    update(game) {
      glow.color.lerpColors(from, to, 0.5 + 0.5 * Math.sin(game.t * 4));
    },
    dispose: () => glow.dispose(),
  };
}

function createGems(gems, assets) {
  const group = new THREE.Group();
  const meshes = gems.map((gem, i) => {
    const mesh = at(new THREE.Mesh(assets.gem, assets.gems[i % assets.gems.length]), gem.x, gem.y, SPRITE_Z);
    group.add(mesh);
    return mesh;
  });
  return {
    object: group,
    update(game) {
      gems.forEach((gem, i) => {
        meshes[i].visible = !gem.collected && !gem.lost;
        meshes[i].position.y = gem.y + 0.08 * Math.sin(game.t * 3 + i);
        meshes[i].rotation.y = game.t * 2 + i;
      });
    },
  };
}

function createEnemySprites(enemies, assets) {
  const group = new THREE.Group();
  const mites = enemies.mites.map(() => {
    const sprite = new THREE.Group();
    sprite.add(
      at(block(assets, assets.mite, MITE_HALF_WIDTH * 2, MITE_HEIGHT), 0, MITE_HEIGHT / 2, 0),
      at(block(assets, assets.lamp, 0.1, 0.1), 0.26, 0.34),
      at(block(assets, assets.lamp, 0.1, 0.1), 0.1, 0.34),
    );
    group.add(sprite);
    return sprite;
  });
  const bats = enemies.bats.map(() => {
    const wings = [-1, 1].map((side) =>
      at(block(assets, assets.bat, BAT_HALF_WIDTH * 1.2, 0.08), side * BAT_HALF_WIDTH, 0),
    );
    const sprite = new THREE.Group();
    sprite.add(block(assets, assets.bat, 0.28, BAT_HALF_HEIGHT * 2), ...wings);
    group.add(sprite);
    return { sprite, wings };
  });
  return {
    object: group,
    update(game) {
      enemies.mites.forEach((mite, i) => {
        mites[i].visible = mite.alive;
        mites[i].position.set(mite.x, mite.y, SPRITE_Z);
        mites[i].scale.x = mite.dir;
      });
      enemies.bats.forEach((bat, i) => {
        const { sprite, wings } = bats[i];
        sprite.visible = bat.alive;
        sprite.position.set(bat.x, bat.y, SPRITE_Z);
        const flap = 0.6 * Math.sin(game.t * 18 + i);
        wings[0].rotation.z = flap;
        wings[1].rotation.z = -flap;
      });
    },
  };
}

function createPlayerSprites(players, assets) {
  const group = new THREE.Group();
  const sprites = players.map((player) => {
    const sprite = new THREE.Group();
    sprite.add(
      block(assets, assets.players[player.index], T.playerHalfWidth * 2, T.playerHalfHeight * 2),
      at(block(assets, assets.helmet, 0.78, 0.22), 0, 0.42),
      at(block(assets, assets.lamp, 0.16, 0.12), 0.38, 0.42, 0.1),
      at(block(assets, assets.eye, 0.1, 0.14), 0.18, 0.12),
    );
    group.add(sprite);
    return sprite;
  });
  return {
    object: group,
    update(game) {
      players.forEach((player, i) => {
        const blinking = player.invulnerable > 0 && Math.floor(game.t * 12) % 2 === 1;
        sprites[i].visible = !player.dead && !blinking;
        sprites[i].position.set(player.x, player.y, SPRITE_Z);
        sprites[i].scale.x = player.facing;
      });
    },
  };
}

/** The dark wall of the cave-in, rubble pouring off its edge, and a shower on every rumble. */
function createCaveInEffects(game, assets, rng, rubble) {
  const wall = new THREE.Mesh(assets.plane, assets.caveIn);
  wall.scale.set(80, T.viewHeight + 4, 1);
  let pending = 0;
  let rumbles = game.rumble.count;
  return {
    object: wall,
    update(game, dt) {
      const edge = game.caveIn.x;
      wall.position.set(edge - 40, T.viewHeight / 2, CAVE_IN_Z);
      if (game.t >= T.caveInDelay && !game.complete) {
        for (pending += dt * 14; pending >= 1; pending -= 1) {
          rubble.spawn(edge + rng.range(-0.8, 0.2), rng.range(6, 11), rng.range(-0.5, 0.5), rng.range(-2, 0));
        }
      }
      if (game.rumble.count !== rumbles) {
        rumbles = game.rumble.count;
        for (let n = 0; n < 18; n++) {
          rubble.spawn(game.camera.x + rng.range(-10, 10), T.viewHeight - 1.2, rng.range(-1, 1), rng.range(-3, 0));
        }
      }
    },
  };
}

function createRubble(assets, rng) {
  const mesh = new THREE.InstancedMesh(assets.rubble, assets.white, MAX_RUBBLE);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false;
  const color = new THREE.Color();
  const pieces = [];
  for (let i = 0; i < MAX_RUBBLE; i++) {
    mesh.setColorAt(i, color.setHex(COLOR.rubble).offsetHSL(0, 0, rng.range(-0.06, 0.06)));
    pieces.push({ alive: false, x: 0, y: 0, vx: 0, vy: 0, angle: 0, spin: 0 });
  }
  const dummy = new THREE.Object3D();
  let next = 0;
  return {
    object: mesh,
    spawn(x, y, vx, vy) {
      Object.assign(pieces[next], { alive: true, x, y, vx, vy, angle: 0, spin: rng.range(-6, 6) });
      next = (next + 1) % MAX_RUBBLE;
    },
    update(game, dt) {
      pieces.forEach((piece, i) => {
        if (piece.alive) {
          piece.vy -= T.gravity * dt;
          piece.x += piece.vx * dt;
          piece.y += piece.vy * dt;
          piece.angle += piece.spin * dt;
          piece.alive = piece.y > -1;
        }
        dummy.position.set(piece.x, piece.y, CAVE_IN_Z + 0.5); // spills over the cave-in edge
        dummy.rotation.set(0, 0, piece.angle);
        dummy.scale.setScalar(piece.alive ? 1 : 0);
        dummy.updateMatrix();
        mesh.setMatrixAt(i, dummy.matrix);
      });
      mesh.instanceMatrix.needsUpdate = true;
    },
    dispose: () => mesh.dispose(),
  };
}
