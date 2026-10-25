// Smoke-test fixture: a minimal game that honours the harness contract but whose index.html
// links a stylesheet from a CDN. The request is blocked, so only the no-errors check fails.
const held = new Set();
window.addEventListener("keydown", (event) => {
  held.add(event.code);
  event.preventDefault();
});
window.addEventListener("keyup", (event) => held.delete(event.code));

const player = { x: 0, y: 0.5, vx: 0, vy: 0, grounded: true };
let t = 0;

window.iterplay = {
  version: "1",
  ready: false,
  getState: () => ({
    t,
    player: { ...player },
    goal: { x: 12, y: 0.5 },
    collectibles: [],
    deaths: 0,
    levelComplete: false,
    fps: 60,
    events: [],
  }),
  reset() {},
};

function frame() {
  t += 1 / 60;
  player.vx = (held.has("ArrowRight") ? 5 : 0) - (held.has("ArrowLeft") ? 5 : 0);
  player.x += player.vx / 60;
  window.iterplay.ready = true;
  window.requestAnimationFrame(frame);
}
window.requestAnimationFrame(frame);
