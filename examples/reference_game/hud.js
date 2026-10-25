// DOM overlay: progress, the cave-in warning, the finish banner and, with ?harness=1, the
// clock and bot name the iterplay video critic reads.
import { TUNING as T } from "./tuning.js";

const WARNING_DISTANCE = 6;

export class Hud {
  #elements;
  #shown = new Map();
  #botName;

  constructor(root, { harness, bot }) {
    this.#elements = {
      stats: root.querySelector("#hud-stats"),
      harness: root.querySelector("#hud-harness"),
      warning: root.querySelector("#hud-warning"),
      banner: root.querySelector("#hud-banner"),
      title: root.querySelector("#hud-banner h1"),
      detail: root.querySelector("#hud-banner p"),
    };
    this.#elements.harness.hidden = !harness;
    this.#botName = bot || "human";
  }

  update(game) {
    const clock = formatClock(game.t);
    const total = game.level.gems.length;
    this.#text("stats", game.players.map((p) => playerStats(p, game, total)).join("   ") + `   ${clock}`);
    this.#text("harness", `${clock}  ${this.#botName}`);
    this.#elements.warning.hidden = game.complete || !caveInClose(game);
    this.#elements.banner.hidden = !game.complete;
    if (!game.complete) return;
    if (game.players.length > 1) {
      this.#text("title", `PLAYER ${game.winner + 1} WINS`);
      this.#text("detail", `${clock} · press R or Enter to race again`);
    } else {
      this.#text("title", "TUNNEL CLEARED");
      this.#text("detail", `${clock} · ${game.players[0].gems}/${total} gems · press R to run again`);
    }
  }

  #text(name, value) {
    if (this.#shown.get(name) === value) return;
    this.#shown.set(name, value);
    this.#elements[name].textContent = value;
  }
}

function playerStats(player, game, total) {
  const label = game.players.length > 1 ? `P${player.index + 1} ` : "";
  return `${label}GEMS ${player.gems}/${total}  DEATHS ${player.deaths}`;
}

function caveInClose(game) {
  if (game.t < T.caveInDelay) return false;
  return game.players.some((p) => !p.dead && p.x - game.caveIn.x < WARNING_DISTANCE);
}

function formatClock(seconds) {
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}
