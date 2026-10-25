"""Bot personas (HLD §4 [3]) steer, jump and recover as designed, and stay reproducible.

Most tests drive the bots with synthetic ``getState()`` sequences ticking at 10 Hz, like the
playtest runner. The browser tests play the reference level's real simulation in Chromium.
"""

import math
import random
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from itertools import groupby
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest
from playwright.async_api import Page, Route, async_playwright

from iterplay.bots.base import Bot
from iterplay.bots.newbie import NewbieBot
from iterplay.bots.random_bot import JUMP_CHANCE, RESPAWN_CHANCE, RandomBot
from iterplay.bots.registry import BOT_TYPES
from iterplay.bots.speedrunner import GIVE_UP_AFTER_S, RESPAWN_AFTER_S, SpeedrunnerBot
from iterplay.contract.controls import CONTROLS_2D, Control, ControlScheme
from iterplay.contract.layout import ALLOWED_PATH
from iterplay.contract.state import GameState
from iterplay.models.playtest import BotName

TICK: Final = 0.1
LEFT, RIGHT, FORWARD, BACK = Control.LEFT, Control.RIGHT, Control.FORWARD, Control.BACK
JUMP, RESPAWN = Control.JUMP, Control.RESPAWN

CONTROLS_3D: Final = ControlScheme(
    bindings=MappingProxyType(
        {
            LEFT: ("ArrowLeft", "KeyA"),
            RIGHT: ("ArrowRight", "KeyD"),
            FORWARD: ("ArrowUp", "KeyW"),
            BACK: ("ArrowDown", "KeyS"),
            JUMP: ("Space",),
            RESPAWN: ("KeyR",),
        }
    ),
    directions=MappingProxyType(
        {
            LEFT: (-1.0, 0.0, 0.0),
            RIGHT: (1.0, 0.0, 0.0),
            FORWARD: (0.0, 0.0, -1.0),
            BACK: (0.0, 0.0, 1.0),
        }
    ),
    smoke_move=FORWARD,
)
"""What a 3D template's scheme could look like, with forward along -z."""

MOVE_ONLY: Final = ControlScheme(
    bindings=MappingProxyType({LEFT: ("ArrowLeft",), RIGHT: ("ArrowRight",)}),
    directions=CONTROLS_2D.directions,
    smoke_move=RIGHT,
)
"""A scheme that binds neither jump nor respawn."""


def gem(
    gem_id: str, x: float, y: float = 0.0, *, z: float = 0.0, collected: bool = False
) -> dict[str, Any]:
    return {"id": gem_id, "x": x, "y": y, "z": z, "collected": collected}


def snapshot(
    tick: int,
    *,
    x: float = 0.0,
    y: float = 0.0,
    vy: float = 0.0,
    grounded: bool = True,
    goal: Sequence[float] = (50.0, 0.0),
    collectibles: Sequence[dict[str, Any]] = (),
    deaths: int = 0,
    events: Sequence[str] = (),
    complete: bool = False,
) -> GameState:
    """A ``getState()`` snapshot taken at ``tick``, with game time matching the video clock."""
    t = tick * TICK
    return GameState.model_validate(
        {
            "t": t,
            "player": {"x": x, "y": y, "vx": 0, "vy": vy, "grounded": grounded},
            "goal": dict(zip("xyz", goal, strict=False)),
            "collectibles": list(collectibles),
            "deaths": deaths,
            "levelComplete": complete,
            "fps": 60,
            "events": [{"type": event, "t": t} for event in events],
        }
    )


def still(ticks: int, *, start: int = 0, **fields: Any) -> list[GameState]:
    """The player standing still, as when running into a wall."""
    return [snapshot(tick, **fields) for tick in range(start, start + ticks)]


def walking(ticks: int, *, speed: float = 0.7, **fields: Any) -> list[GameState]:
    """The player running right from x = 0, by default at 7 world units per second."""
    return [snapshot(tick, x=speed * tick, **fields) for tick in range(ticks)]


def wandering(ticks: int, seed: int = 0) -> list[GameState]:
    """An erratic run for property tests: runs, stalls, hops, pickups, deaths and respawns."""
    rng = random.Random(seed)
    items = [gem(f"g{i}", rng.uniform(-20, 40), rng.choice((0.0, 0.0, 3.0))) for i in range(8)]
    x = y = vy = 0.0
    deaths = 0
    states: list[GameState] = []
    for tick in range(ticks):
        events: list[str] = []
        stuck = 100 <= tick < 200 or rng.random() < 0.2
        if not stuck:
            x += rng.uniform(-0.7, 0.7)
        if y == 0 and rng.random() < 0.08:
            vy = 7.0
        y = max(0.0, y + vy * TICK)
        vy = vy - 3.8 if y > 0 else 0.0
        if rng.random() < 0.01:
            deaths += 1
            events = ["death", "respawn"]
            x = y = vy = 0.0
        for item in items:
            if not item["collected"] and math.dist((x, y), (item["x"], item["y"])) < 0.7:
                item["collected"] = True
                events.append("collect")
        states.append(
            snapshot(
                tick,
                x=x,
                y=y,
                vy=vy,
                grounded=y == 0,
                collectibles=[dict(item) for item in items],
                deaths=deaths,
                events=events,
            )
        )
    return states


def play(bot: Bot, states: Sequence[GameState]) -> list[frozenset[Control]]:
    return [bot.decide(state, state.t) for state in states]


def ticks_with(decisions: Sequence[frozenset[Control]], control: Control) -> list[int]:
    return [tick for tick, held in enumerate(decisions) if control in held]


def tick_at(seconds: float) -> int:
    return round(seconds / TICK)


def calm_newbie(
    *,
    input_lag_s: float = 0.0,
    overshoot_s: float = 0.0,
    pause_chance: float = 0.0,
    pause_s: tuple[float, float] = (0.5, 2.0),
) -> NewbieBot:
    """A Newbie with only the flaws a test asks for."""
    return NewbieBot(
        1,
        CONTROLS_2D,
        input_lag_s=input_lag_s,
        overshoot_s=overshoot_s,
        pause_chance=pause_chance,
        pause_s=pause_s,
    )


def random_decisions(
    seed: int, ticks: int, controls: ControlScheme = CONTROLS_2D, **holds: float
) -> list[frozenset[Control]]:
    bot = RandomBot(seed, controls, **holds)
    return [bot.decide(snapshot(tick), tick * TICK) for tick in range(ticks)]


# Speedrunner: where it steers


def test_speedrunner_steers_toward_the_nearest_uncollected_collectible() -> None:
    items = [gem("far", 5.0), gem("near", -3.0), gem("taken", 1.0, collected=True)]

    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, collectibles=items), 0.0)

    assert held == {LEFT}


def test_speedrunner_measures_distance_along_every_axis() -> None:
    items = [gem("high", 1.0, 9.0), gem("deep", 1.5, z=9.0), gem("level", -4.0)]

    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, collectibles=items), 0.0)

    assert held == {LEFT}


def test_speedrunner_moves_on_after_each_pickup_and_then_heads_for_the_goal() -> None:
    first, second = gem("first", 3.0), gem("second", -5.0)
    states = [
        snapshot(0, collectibles=[first, second], goal=(20.0, 0.0)),
        snapshot(1, collectibles=[{**first, "collected": True}, second], goal=(20.0, 0.0)),
        snapshot(
            2,
            collectibles=[{**first, "collected": True}, {**second, "collected": True}],
            goal=(20.0, 0.0),
        ),
    ]

    assert play(SpeedrunnerBot(1, CONTROLS_2D), states) == [{RIGHT}, {LEFT}, {RIGHT}]


def test_speedrunner_heads_for_the_goal_when_there_is_nothing_to_collect() -> None:
    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, goal=(-8.0, 3.0)), 0.0)

    assert held == {LEFT}


def test_speedrunner_does_not_steer_inside_the_dead_zone() -> None:
    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, goal=(0.15, 4.0)), 0.0)

    assert held == set()


def test_speedrunner_stops_steering_near_a_collectible_to_let_the_pickup_happen() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), still(5, collectibles=[gem("close", 0.6)]))

    assert held == [set()] * 5


def test_speedrunner_creeps_closer_once_a_pickup_stalls() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), still(6, collectibles=[gem("close", 0.6)]))

    assert held[4] == set()
    assert held[5] == {RIGHT, JUMP}


def test_speedrunner_keeps_steering_all_the_way_into_the_goal() -> None:
    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, goal=(0.6, 0.0)), 0.0)

    assert held == {RIGHT}


def test_speedrunner_steers_with_every_world_direction_of_a_3d_scheme() -> None:
    held = SpeedrunnerBot(1, CONTROLS_3D).decide(snapshot(0, goal=(10.0, 0.0, -10.0)), 0.0)

    assert held == {RIGHT, FORWARD}


def test_speedrunner_holds_nothing_once_the_level_is_complete() -> None:
    held = SpeedrunnerBot(1, CONTROLS_2D).decide(snapshot(0, complete=True), 0.0)

    assert held == set()


# Speedrunner: when it jumps


def test_speedrunner_jumps_when_blocked_and_holds_each_jump() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), still(16))

    assert all(RIGHT in decision for decision in held)
    assert ticks_with(held, JUMP) == [5, 6, 7, 10, 11, 12, 15]


def test_speedrunner_waits_blocked_after_s_before_jumping() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D, blocked_after_s=0.2), still(4))

    assert ticks_with(held, JUMP) == [2, 3]


def test_speedrunner_does_not_jump_while_making_progress() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), walking(30))

    assert held == [{RIGHT}] * 30


def test_speedrunner_only_jumps_from_the_ground_when_blocked() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), still(20, grounded=False))

    assert ticks_with(held, JUMP) == []


def test_speedrunner_holds_a_jump_for_as_long_as_the_player_rises() -> None:
    rising = [snapshot(tick, grounded=False, vy=18.0 - 2 * tick) for tick in range(6, 12)]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*still(6), *rising])

    assert ticks_with(held, JUMP) == [5, 6, 7, 8]


def test_speedrunner_releases_a_rising_jump_after_max_jump_hold() -> None:
    rising = [snapshot(tick, grounded=False, vy=3.0) for tick in range(6, 25)]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*still(6), *rising])

    assert ticks_with(held, JUMP) == list(range(5, 15))


def test_speedrunner_jumps_on_walking_off_a_ledge() -> None:
    falling = [
        snapshot(tick, x=0.7 * tick, grounded=False, vy=-2.0 * (tick - 3)) for tick in range(4, 9)
    ]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*walking(4), *falling])

    assert ticks_with(held, JUMP) == [4, 5, 6]


def test_speedrunner_does_not_jump_when_launched_upward() -> None:
    launched = [snapshot(tick, x=0.7 * tick, grounded=False, vy=6.0) for tick in range(4, 8)]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*walking(4), *launched])

    assert ticks_with(held, JUMP) == []


def test_speedrunner_does_not_mistake_its_own_jump_for_a_ledge() -> None:
    """The game takes a tick to react to the jump, then the player bumps a low ceiling."""
    bumped = [snapshot(6), snapshot(7, grounded=False), *still(3, start=8, grounded=False, vy=-3.0)]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*still(6), *bumped])

    assert ticks_with(held, JUMP) == [5, 6, 7]


def test_speedrunner_hops_back_after_respawning_until_it_beats_its_best() -> None:
    """Running back from a checkpoint is not progress, so it jumps whenever it is on the
    ground, like the prototype that finishes the reference level."""
    ahead = walking(10)
    back = [snapshot(10 + i, x=0.7 * i, events=["respawn"] if i == 0 else ()) for i in range(10)]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), [*ahead, *back])

    assert ticks_with(held, JUMP) == [14, 15, 16, 19]
    assert ticks_with(held, RESPAWN) == []


# Speedrunner: recovering from stalls


def test_speedrunner_gives_up_on_an_unreachable_collectible() -> None:
    out_of_reach = [gem("ledge", 3.0, 10.0)]
    give_up = tick_at(GIVE_UP_AFTER_S)

    held = play(
        SpeedrunnerBot(1, CONTROLS_2D),
        still(give_up + 20, collectibles=out_of_reach, goal=(-20.0, 0.0)),
    )

    assert all(RIGHT in decision for decision in held[:give_up] if RESPAWN not in decision)
    assert all(LEFT in decision for decision in held[give_up:])


def test_speedrunner_presses_respawn_once_per_long_stall() -> None:
    stall = tick_at(RESPAWN_AFTER_S)

    held = play(SpeedrunnerBot(1, CONTROLS_2D), still(2 * stall + 5))

    presses = ticks_with(held, RESPAWN)
    assert presses[0] == stall
    assert len(presses) == 2
    assert presses[1] - presses[0] >= stall
    assert all(held[tick] == {RESPAWN} for tick in presses)


def test_speedrunner_never_respawns_while_making_progress() -> None:
    held = play(SpeedrunnerBot(1, CONTROLS_2D), walking(tick_at(30.0), speed=0.1))

    assert ticks_with(held, RESPAWN) == []


@pytest.mark.parametrize(
    "signal",
    [{"events": ["death"]}, {"events": ["respawn"]}, {"deaths": 1}],
    ids=["death event", "respawn event", "death count"],
)
def test_speedrunner_restarts_its_stall_clock_when_the_player_respawns(
    signal: dict[str, Any],
) -> None:
    respawned = tick_at(6.0)
    deaths = signal.get("deaths", 0)
    states = [
        *still(respawned),
        snapshot(respawned, **signal),
        *still(tick_at(10.0), start=respawned + 1, deaths=deaths),
    ]

    held = play(SpeedrunnerBot(1, CONTROLS_2D), states)

    assert ticks_with(held, RESPAWN) == [respawned + tick_at(RESPAWN_AFTER_S)]


# Random


@pytest.mark.parametrize("hold_s", [0.2, 0.5, 1.5])
def test_random_bot_changes_its_mind_only_when_a_hold_ends(hold_s: float) -> None:
    held = random_decisions(3, 300, min_hold_s=hold_s, max_hold_s=hold_s)

    period = tick_at(hold_s)
    for start in range(0, len(held), period):
        assert len({decision - {RESPAWN} for decision in held[start : start + period]}) == 1


def test_random_bot_holds_each_combination_for_at_least_min_hold() -> None:
    held = random_decisions(5, 600, min_hold_s=0.5, max_hold_s=1.5)

    runs = [len(list(run)) for _, run in groupby(decision - {RESPAWN} for decision in held)]

    assert min(runs[:-1]) >= tick_at(0.5)


def test_random_bot_jumps_in_about_two_fifths_of_its_holds() -> None:
    holds = random_decisions(11, 6000, min_hold_s=0.5, max_hold_s=0.5)[:: tick_at(0.5)]

    share = sum(JUMP in held for held in holds) / len(holds)

    assert share == pytest.approx(JUMP_CHANCE, abs=0.05)


def test_random_bot_rarely_taps_respawn_and_only_as_a_hold_starts() -> None:
    period = tick_at(0.5)
    held = random_decisions(13, 6000, min_hold_s=0.5, max_hold_s=0.5)

    taps = ticks_with(held, RESPAWN)

    assert taps
    assert all(tick % period == 0 for tick in taps)
    assert len(taps) / (len(held) / period) <= RESPAWN_CHANCE + 0.01


def test_random_bot_tries_every_movement_combination() -> None:
    holds = random_decisions(17, 3000, min_hold_s=0.5, max_hold_s=0.5)[:: tick_at(0.5)]

    moves = Counter(held & {LEFT, RIGHT} for held in holds)

    assert set(moves) == {frozenset(), frozenset({LEFT}), frozenset({RIGHT})}
    assert min(moves.values()) / len(holds) > 0.25


def test_random_bot_combines_directions_that_are_not_opposites() -> None:
    held = random_decisions(19, 1000, CONTROLS_3D)

    assert any({RIGHT, FORWARD} <= decision for decision in held)
    assert not any({FORWARD, BACK} <= decision or {LEFT, RIGHT} <= decision for decision in held)


# Newbie


def test_newbie_acts_on_the_plan_from_input_lag_earlier() -> None:
    states = wandering(300)
    lag = tick_at(0.3)
    planned = play(SpeedrunnerBot(1, CONTROLS_2D), states)

    acted = play(calm_newbie(input_lag_s=0.3), states)

    assert acted[:lag] == [set()] * lag
    assert acted[lag:] == planned[:-lag]


def test_newbie_keeps_holding_a_direction_after_the_planner_lets_go() -> None:
    reaching = [snapshot(tick, x=0.7 * tick, goal=(10.0, 0.0)) for tick in range(5)]
    arrived = [snapshot(tick, x=3.5, goal=(3.5, 0.0)) for tick in range(5, 10)]
    states = [*reaching, *arrived]

    planned = play(SpeedrunnerBot(1, CONTROLS_2D), states)
    acted = play(calm_newbie(overshoot_s=0.25), states)

    assert ticks_with(planned, RIGHT) == [0, 1, 2, 3, 4]
    assert ticks_with(acted, RIGHT) == [0, 1, 2, 3, 4, 5, 6, 7]


def test_newbie_overshoot_delays_reversing() -> None:
    there = [snapshot(tick, x=0.7 * tick, goal=(10.0, 0.0)) for tick in range(5)]
    back = [snapshot(tick, x=3.5, goal=(-10.0, 0.0)) for tick in range(5, 12)]

    acted = play(calm_newbie(overshoot_s=0.25), [*there, *back])

    assert ticks_with(acted, RIGHT) == [0, 1, 2, 3, 4, 5, 6, 7]
    assert ticks_with(acted, LEFT) == [8, 9, 10, 11]


def test_newbie_holds_nothing_while_paused() -> None:
    held = play(calm_newbie(pause_chance=1.0), walking(50, goal=(1000.0, 0.0)))

    assert held == [set()] * 50


def test_newbie_pauses_start_by_chance_and_last_their_drawn_duration() -> None:
    held = play(
        calm_newbie(pause_chance=0.05, pause_s=(0.5, 1.0)), walking(1200, goal=(10_000.0, 0.0))
    )

    pauses = [
        len(list(run)) for paused, run in groupby(not decision for decision in held) if paused
    ]

    assert 20 <= len(pauses) <= 60
    assert min(pauses) >= tick_at(0.5)
    assert sorted(pauses)[len(pauses) // 2] <= tick_at(1.0)
    assert all(decision in ({RIGHT}, set()) for decision in held)


def test_newbie_presses_respawn_only_to_recover_from_a_long_stall() -> None:
    held = play(calm_newbie(input_lag_s=0.3), still(tick_at(RESPAWN_AFTER_S) + 10))

    assert ticks_with(held, RESPAWN) == [tick_at(RESPAWN_AFTER_S + 0.3)]


def test_newbie_holds_nothing_once_the_level_is_complete() -> None:
    bot = NewbieBot(1, CONTROLS_2D)
    states = [*walking(10), snapshot(10, x=7.0, complete=True)]

    assert play(bot, states)[-1] == set()


# Every persona


@pytest.mark.parametrize("name", list(BotName))
def test_bots_are_reproducible_for_a_seed(name: BotName) -> None:
    states = wandering(400)

    first = play(BOT_TYPES[name](7, CONTROLS_2D), states)
    again = play(BOT_TYPES[name](7, CONTROLS_2D), states)

    assert first == again


@pytest.mark.parametrize("name", [BotName.RANDOM, BotName.NEWBIE])
def test_seeds_vary_the_random_personas(name: BotName) -> None:
    states = wandering(400)

    assert play(BOT_TYPES[name](1, CONTROLS_2D), states) != play(
        BOT_TYPES[name](2, CONTROLS_2D), states
    )


@pytest.mark.parametrize(
    "controls", [CONTROLS_2D, CONTROLS_3D, MOVE_ONLY], ids=["2d", "3d", "move"]
)
@pytest.mark.parametrize("name", list(BotName))
def test_bots_hold_only_bound_controls_and_never_opposites(
    name: BotName, controls: ControlScheme
) -> None:
    for seed in range(5):
        for held in play(BOT_TYPES[name](seed, controls), wandering(300, seed)):
            assert held <= set(controls.bindings)
            assert not {LEFT, RIGHT} <= held
            assert not {FORWARD, BACK} <= held


# The reference level, in Chromium

REFERENCE_GAME: Final = Path(__file__).resolve().parents[1] / "examples" / "reference_game"

REFERENCE_ORIGIN: Final = "http://127.0.0.1:9"
"""Origin the page loads from. Every request is fulfilled from disk, so nothing listens here."""

SIMULATION_PAGE: Final = """<!doctype html>
<script type="module">
  import { Game } from "./game.js";
  let game;
  let previous = new Set();
  window.simulation = {
    start(seed) {
      game = new Game({ seed });
      previous = new Set();
      return game.snapshot(60);
    },
    tick(controls) {
      const held = new Set(controls);
      for (let frame = 0; frame < 6 && !game.complete; frame++) {
        const pressed = (control) => frame === 0 && held.has(control) && !previous.has(control);
        game.step(1 / 60, [{
          left: held.has("left"),
          right: held.has("right"),
          jumpHeld: held.has("jump"),
          jumpPressed: pressed("jump"),
          respawnPressed: pressed("respawn"),
        }]);
      }
      previous = held;
      return game.snapshot(60);
    },
  };
  window.simulationReady = true;
</script>
"""
"""Steps the level's pure simulation six 1/60 s frames per bot tick, as fixedStep does at 60 fps."""


async def serve_reference_game(route: Route) -> None:
    name = route.request.url.removeprefix(f"{REFERENCE_ORIGIN}/")
    if not name:
        await route.fulfill(body=SIMULATION_PAGE, content_type="text/html")
    elif ALLOWED_PATH.fullmatch(name) and name.endswith(".js"):
        await route.fulfill(path=REFERENCE_GAME / name, content_type="text/javascript")
    else:
        await route.abort()


@pytest.fixture
async def reference_level() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route(f"{REFERENCE_ORIGIN}/**", serve_reference_game)
            await page.goto(f"{REFERENCE_ORIGIN}/")
            await page.wait_for_function("window.simulationReady === true")
            yield page
        finally:
            await browser.close()


async def play_reference_level(
    page: Page, bot: Bot, seed: int
) -> tuple[GameState, list[frozenset[Control]]]:
    """One run of at most 60 s at 10 Hz: the last snapshot and every decision."""
    state = GameState.model_validate(await page.evaluate("s => window.simulation.start(s)", seed))
    decisions: list[frozenset[Control]] = []
    for tick in range(tick_at(60.0)):
        if state.level_complete:
            break
        held = bot.decide(state, tick * TICK)
        decisions.append(held)
        controls = [control.value for control in held]
        state = GameState.model_validate(
            await page.evaluate("c => window.simulation.tick(c)", controls)
        )
    return state, decisions


@pytest.mark.browser
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_speedrunner_finishes_the_reference_level(reference_level: Page, seed: int) -> None:
    final, decisions = await play_reference_level(
        reference_level, SpeedrunnerBot(seed, CONTROLS_2D), seed
    )

    assert final.level_complete
    assert all(item.collected for item in final.collectibles)
    assert final.deaths <= 4
    assert len(decisions) * TICK <= 40
    assert ticks_with(decisions, RESPAWN) == []


@pytest.mark.browser
async def test_newbie_finishes_the_reference_level_on_most_seeds(reference_level: Page) -> None:
    finals = [
        (await play_reference_level(reference_level, NewbieBot(seed, CONTROLS_2D), seed))[0]
        for seed in range(1, 5)
    ]

    assert sum(final.level_complete for final in finals) >= 3
