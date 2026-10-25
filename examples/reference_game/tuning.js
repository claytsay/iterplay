// Every feel and difficulty knob in one place. Units are tiles (1 tile = 1 world unit) and
// seconds.
export const TUNING = Object.freeze({
  // Running and jumping. A full jump is about 2.6 tiles high and 5 tiles long at full speed.
  runSpeed: 7,
  groundAccel: 60,
  groundDecel: 55,
  airAccel: 35,
  gravity: 38,
  maxFallSpeed: 22,
  jumpSpeed: 14,
  jumpCutFactor: 0.5, // upward speed kept when jump is released early (hold ~0.3 s for full height)
  minJumpTime: 0.08, // a tap still gives at least this much of a full jump
  coyoteTime: 0.12,
  jumpBuffer: 0.12,
  stepUp: 0.45, // lips this low are climbed without jumping

  playerHalfWidth: 0.35,
  playerHalfHeight: 0.45,

  // Enemies knock players back instead of killing them: the race is the danger.
  knockbackX: 7,
  knockbackY: 7,
  stunTime: 0.35,
  invulnerableTime: 1,
  miteSpeed: 1.6,
  mitePatrol: 3.5,
  batSwingX: 2.5,
  batSwingY: 0.6,
  batSpeed: 1.2, // radians per second

  // The cave-in that chases the players from the left.
  caveInStart: -3, // the start (x = 2.5) begins safely ahead of it
  caveInDelay: 4,
  caveInSpeed: 1.6,
  respawnMargin: 5, // players respawn at least this far ahead of the cave-in

  // Cracked rock and rumbles.
  crumbleDelay: 0.45,
  rumbleMinInterval: 7,
  rumbleMaxInterval: 10,
  rumbleDuration: 0.8,
  rumbleCracks: 3,
  rumbleReach: 30,

  respawnDelay: 0.5,

  // Camera: the whole tunnel height is always in view.
  viewHeight: 12,
  viewAspect: 16 / 9,
  lookAhead: 3,
});
