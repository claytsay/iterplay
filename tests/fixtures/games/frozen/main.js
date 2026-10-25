// Smoke-test fixture: a game whose getState() is valid but whose player never moves, because
// it ignores the keyboard. Only the movement check fails.
let t = 0;

window.iterplay = {
  version: "1",
  ready: false,
  getState: () => ({
    t,
    player: { x: 0, y: 0.5, vx: 0, vy: 0, grounded: true },
    goal: { x: 12, y: 0.5 },
    collectibles: [{ id: "g1", x: 3, y: 0.5, collected: false }],
    deaths: 0,
    levelComplete: false,
    fps: 60,
    events: [],
  }),
  reset() {},
};

function frame() {
  t += 1 / 60;
  window.iterplay.ready = true;
  window.requestAnimationFrame(frame);
}
window.requestAnimationFrame(frame);
