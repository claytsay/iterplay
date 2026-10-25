// Smoke-test fixture: a game that becomes ready but whose getState() breaks the schema: the
// time is a string, the player lacks most fields, and the goal and collectibles are missing.
// The state-schema check fails, so the movement check is skipped.
window.iterplay = {
  version: "1",
  ready: true,
  getState: () => ({ t: "soon", player: { x: 0 }, levelComplete: "maybe", fps: 60, events: [] }),
  reset() {},
};
