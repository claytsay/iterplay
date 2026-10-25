// The tunnel: a hand-authored ASCII map and the mutable tile grid built from it.
//
//   #  rock           =  cracked rock: gives way shortly after being stood on
//   *  gem            m  rock mite: patrols the floor     b  bat: swoops back and forth
//   c  lantern        S  start                           E  exit
//
// Row 0 is the cave roof. In world space x grows to the right and y grows upward, one tile
// per unit, with the bottom row at y = 0. The game is 2D: nothing moves along z.
export const TILE = Object.freeze({ EMPTY: 0, ROCK: 1, CRACKED: 2 });

const MAP = [
  "####################################################################################################",
  "#............##...............##.............#................##..............########.#...........#",
  "#.............................................................................########.............#",
  "#.............................................................................########.............#",
  "#.............................................................................########.............#",
  "#.....................*...........................b...........................########.............#",
  "#.........................................................b........................................#",
  "#.......*...............m.......................................................*...........*......#",
  "#...................########................*..............................................##......#",
  "#.S..*.*........############..........*..c.........##.......*......*......c.......m.....c####....E.#",
  "##########..######################...###########...##...########===##===############################",
  "##########..######################...###########...##...########...##...############################",
];

export class Level {
  constructor(map = MAP) {
    this.height = map.length;
    this.width = map[0].length;
    this.tiles = new Uint8Array(this.width * this.height);
    this.crumbling = new Map(); // tile index -> seconds since a player first stood on it
    this.gems = [];
    this.mites = [];
    this.bats = [];
    this.lanterns = [];
    this.start = null;
    this.exit = null;
    this.#parse(map);
  }

  index(x, y) {
    return y * this.width + x;
  }

  tileAt(x, y) {
    if (x < 0 || y < 0) return TILE.EMPTY; // swallowed by the cave-in, or below the tunnel
    if (x >= this.width || y >= this.height) return TILE.ROCK;
    return this.tiles[this.index(x, y)];
  }

  isSolid(x, y) {
    return this.tileAt(x, y) !== TILE.EMPTY;
  }

  /** Height of the floor in column x: the number of solid tiles stacked up from y = 0. */
  floorTop(x) {
    let y = 0;
    while (y < this.height && this.isSolid(x, y)) y += 1;
    return y;
  }

  /** The first spot at or after x where a player can stand on solid, uncracked rock. */
  firstFooting(fromX) {
    for (let x = Math.max(0, Math.ceil(fromX)); x < this.width; x++) {
      const top = this.floorTop(x);
      if (top >= 1 && top < this.height && this.tileAt(x, top - 1) === TILE.ROCK) {
        return { x: x + 0.5, y: top };
      }
    }
    return null;
  }

  crack(x, y) {
    if (this.tileAt(x, y) === TILE.ROCK) this.tiles[this.index(x, y)] = TILE.CRACKED;
  }

  /** A player is standing on (x, y): cracked rock starts to give way. */
  stepOn(x, y) {
    const i = this.index(x, y);
    if (this.tileAt(x, y) === TILE.CRACKED && !this.crumbling.has(i)) this.crumbling.set(i, 0);
  }

  updateCrumbling(dt, delay) {
    for (const [i, elapsed] of this.crumbling) {
      if (elapsed + dt < delay) {
        this.crumbling.set(i, elapsed + dt);
      } else {
        this.tiles[i] = TILE.EMPTY;
        this.crumbling.delete(i);
      }
    }
  }

  collapseColumn(x) {
    for (let y = 0; y < this.height; y++) {
      const i = this.index(x, y);
      this.tiles[i] = TILE.EMPTY;
      this.crumbling.delete(i);
    }
  }

  #parse(map) {
    for (const [row, line] of map.entries()) {
      if (line.length !== this.width) throw new Error(`Map row ${row} is not ${this.width} wide`);
      const y = this.height - 1 - row;
      for (const [x, symbol] of [...line].entries()) {
        const cell = { x: x + 0.5, y };
        switch (symbol) {
          case "#":
            this.tiles[this.index(x, y)] = TILE.ROCK;
            break;
          case "=":
            this.tiles[this.index(x, y)] = TILE.CRACKED;
            break;
          case "*":
            this.gems.push({ x: x + 0.5, y: y + 0.5, collected: false, lost: false });
            break;
          case "m":
            this.mites.push(cell);
            break;
          case "b":
            this.bats.push({ x: x + 0.5, y: y + 0.5 });
            break;
          case "c":
            this.lanterns.push(cell);
            break;
          case "S":
            this.start = cell;
            break;
          case "E":
            this.exit = cell;
            break;
          case ".":
            break;
          default:
            throw new Error(`Unknown map symbol "${symbol}" at row ${row}, column ${x}`);
        }
      }
    }
    if (!this.start || !this.exit) throw new Error("The map needs a start (S) and an exit (E)");
    this.gems.sort((a, b) => a.x - b.x || a.y - b.y);
    this.gems.forEach((gem, i) => (gem.id = `g${i + 1}`));
    this.lanterns.sort((a, b) => a.x - b.x);
  }
}
