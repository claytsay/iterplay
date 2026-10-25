// Keyboard input, read through KeyboardEvent.code so it works on any layout.
//
// One player (the iterplay 2D platformer contract): Left/Right or A/D run, Space, Up or W jump,
// R respawns. Two players (?players=2): A/D, W or Space, and R for player 1; Left/Right, Up and
// Enter for player 2.
const ONE_PLAYER = [
  {
    left: ["ArrowLeft", "KeyA"],
    right: ["ArrowRight", "KeyD"],
    jump: ["Space", "ArrowUp", "KeyW"],
    respawn: ["KeyR"],
  },
];
const TWO_PLAYERS = [
  { left: ["KeyA"], right: ["KeyD"], jump: ["KeyW", "Space"], respawn: ["KeyR"] },
  { left: ["ArrowLeft"], right: ["ArrowRight"], jump: ["ArrowUp"], respawn: ["Enter"] },
];

export class Keyboard {
  #held = new Set();
  #pressed = new Set();
  #bindings;

  constructor(target, playerCount) {
    this.#bindings = playerCount > 1 ? TWO_PLAYERS : ONE_PLAYER;
    const handled = new Set(this.#bindings.flatMap((binding) => Object.values(binding).flat()));
    target.addEventListener("keydown", (event) => {
      if (!handled.has(event.code)) return;
      event.preventDefault();
      if (!event.repeat) this.#pressed.add(event.code);
      this.#held.add(event.code);
    });
    target.addEventListener("keyup", (event) => this.#held.delete(event.code));
    target.addEventListener("blur", () => this.#held.clear());
  }

  /** One intent per player. Presses are consumed, so call this once per simulation step. */
  sample() {
    const any = (codes, keys) => codes.some((code) => keys.has(code));
    const intents = this.#bindings.map((binding) => ({
      left: any(binding.left, this.#held),
      right: any(binding.right, this.#held),
      jumpHeld: any(binding.jump, this.#held),
      jumpPressed: any(binding.jump, this.#pressed),
      respawnPressed: any(binding.respawn, this.#pressed),
    }));
    this.#pressed.clear();
    return intents;
  }
}
