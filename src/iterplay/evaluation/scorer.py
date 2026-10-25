"""[7] Scorer (HLD §4 [7])."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from operator import itemgetter
from typing import Final, TypeAlias

from iterplay.models.playtest import BotName, PlaytestReport
from iterplay.models.rubric import Rubric, RubricItem, RubricItemId, RubricSource
from iterplay.models.scoring import ItemScore, ScoreCard
from iterplay.models.smoke import SmokeReport
from iterplay.models.telemetry import BotTelemetrySummary, TelemetrySummary
from iterplay.models.verification import VerifiedCritique

DEFAULT_MAX_DEATHS: Final = 3
"""``fair_difficulty`` threshold when the rubric item sets no ``max_deaths``."""

DEFAULT_MIN_FPS: Final = 30.0
"""``performance`` threshold when the rubric item sets no ``min_fps``."""

_MAX_ASSESSMENT: Final = 5
"""Top of the critic's 0-5 scale."""

_Measure: TypeAlias = tuple[float | None, str]
"""An item's score (None when unmeasured) and the detail explaining it."""


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    smoke: SmokeReport

    playtest: PlaytestReport | None
    """None when the build never booted; the playtest-based items then score 0."""

    telemetry: TelemetrySummary | None
    verified: VerifiedCritique | None

    headless: bool
    """Headless runs leave ``performance`` unmeasured (HLD §8)."""


class Scorer:
    """Weighted rubric score, mostly from objective telemetry.

    Objective items pass (1) or fail (0), with thresholds from each item's ``params``:

    - ``boots_cleanly``: every smoke check passed.
    - ``no_runtime_errors``: no uncaught errors in any run.
    - ``completable``: the Speedrunner completed the level (any bot, without a Speedrunner run).
    - ``collectibles_reachable``: every collectible was collected by at least one bot.
    - ``collision_integrity``: no unintentional ``oob`` events.
    - ``fair_difficulty``: the Newbie completed the level or died at most ``max_deaths`` times.
    - ``performance``: minimum FPS of at least ``min_fps``, in headed runs only.

    Critic items map the verified 0-5 assessment onto 0-1. The total is the weighted mean of
    the measured items. A build that never booted scores 0 on every item, rather than leaving
    them unmeasured, so it can never outscore one that booted.
    """

    def __init__(self, rubric: Rubric) -> None:
        self._rubric = rubric

    def score(self, version: int, inputs: ScoringInputs) -> ScoreCard:
        """Score every rubric item for ``version``, then total the measured ones."""
        items = [_score_item(item, inputs) for item in self._rubric.items]
        total = _weighted_mean(items)
        return ScoreCard(
            version=version,
            items=items,
            total=total,
            target=self._rubric.target,
            meets_target=total >= self._rubric.target,
        )


def _score_item(item: RubricItem, inputs: ScoringInputs) -> ItemScore:
    score, detail = _measure(item, inputs)
    return ItemScore(
        item_id=item.id, source=item.source, weight=item.weight, score=score, detail=detail
    )


def _measure(item: RubricItem, inputs: ScoringInputs) -> _Measure:
    """Objective items by id, critic items by rating; only ``boots_cleanly`` needs no boot."""
    if item.source is not RubricSource.CRITIC and item.id == RubricItemId.BOOTS_CLEANLY:
        return _boots_cleanly(inputs.smoke)
    if inputs.playtest is None:
        return 0.0, "The build never booted."
    if item.source is RubricSource.CRITIC:
        return _critic_rating(item.id, inputs.verified)
    if item.id == RubricItemId.NO_RUNTIME_ERRORS:
        return _no_runtime_errors(inputs.playtest)
    if inputs.telemetry is None:
        return None, "No telemetry summary to measure it with."
    return _telemetry_measure(item, inputs.telemetry, headless=inputs.headless)


def _telemetry_measure(
    item: RubricItem, telemetry: TelemetrySummary, *, headless: bool
) -> _Measure:
    match item.id:
        case RubricItemId.COMPLETABLE:
            return _completable(telemetry)
        case RubricItemId.FAIR_DIFFICULTY:
            return _fair_difficulty(telemetry, item.params.get("max_deaths", DEFAULT_MAX_DEATHS))
        case RubricItemId.COLLISION_INTEGRITY:
            return _collision_integrity(telemetry)
        case RubricItemId.COLLECTIBLES_REACHABLE:
            return _collectibles_reachable(telemetry)
        case RubricItemId.PERFORMANCE:
            if headless:
                return None, "Not measured: headless browsers render WebGL in software."
            return _performance(telemetry, item.params.get("min_fps", DEFAULT_MIN_FPS))
        case _:
            return None, f"No objective measure for {item.id!r}."


def _boots_cleanly(smoke: SmokeReport) -> _Measure:
    if smoke.passed:
        return 1.0, "Every smoke check passed."
    failed = [result.check.value for result in smoke.checks if not result.passed]
    if not failed:
        return 0.0, f"The smoke test failed on attempt {smoke.attempt}."
    return 0.0, f"Failed smoke checks on attempt {smoke.attempt}: {', '.join(failed)}."


def _no_runtime_errors(playtest: PlaytestReport) -> _Measure:
    errors: Counter[BotName] = Counter()
    for run in playtest.runs:
        errors[run.bot] += len(run.uncaught_errors)
    total = errors.total()
    if total == 0:
        return 1.0, f"No uncaught errors in {_count(len(playtest.runs), 'run')}."
    by_bot = ", ".join(f"{bot} {count}" for bot, count in errors.items() if count)
    return 0.0, f"{_count(total, 'uncaught error')}: {by_bot}."


def _completable(telemetry: TelemetrySummary) -> _Measure:
    speedrunner = _bot(telemetry, BotName.SPEEDRUNNER)
    if speedrunner is not None:
        if speedrunner.completed:
            return 1.0, f"The speedrunner completed the level{_in_time(speedrunner)}."
        return 0.0, "The speedrunner did not complete the level."
    finishers = [bot.bot for bot in telemetry.bots if bot.completed]
    if finishers:
        return 1.0, f"No speedrunner run; completed by {', '.join(finishers)}."
    return 0.0, "No speedrunner run, and no bot completed the level."


def _fair_difficulty(telemetry: TelemetrySummary, max_deaths: float) -> _Measure:
    newbie = _bot(telemetry, BotName.NEWBIE)
    if newbie is None:
        return None, "No newbie run."
    deaths = len(newbie.deaths)
    if newbie.completed:
        return (
            1.0,
            f"The newbie completed the level{_in_time(newbie)} with {_count(deaths, 'death')}.",
        )
    return (
        1.0 if deaths <= max_deaths else 0.0,
        f"The newbie did not finish and died {_count(deaths, 'time')}; "
        f"at most {max_deaths:g} allowed.",
    )


def _collision_integrity(telemetry: TelemetrySummary) -> _Measure:
    falls = [(bot.bot, oob) for bot in telemetry.bots for oob in bot.out_of_bounds]
    unintentional = [(bot, oob) for bot, oob in falls if not oob.intentional]
    pits = len(falls) - len(unintentional)
    ignored = f" ({_count(pits, 'intentional pit fall')} ignored)" if pits else ""
    if not unintentional:
        return 1.0, f"No unintentional oob events{ignored}."
    bot, first = unintentional[0]
    return (
        0.0,
        f"{_count(len(unintentional), 'unintentional oob event')}, the first by the {bot} bot "
        f"at {first.video_t:.1f} s{ignored}.",
    )


def _collectibles_reachable(telemetry: TelemetrySummary) -> _Measure:
    missing = telemetry.never_collected_ids
    collected = {item for bot in telemetry.bots for item in bot.collected_ids}
    total = len(collected | set(missing))
    if missing:
        return (
            0.0,
            f"{len(missing)} of {_count(total, 'collectible')} never collected: "
            f"{', '.join(missing)}.",
        )
    if total == 0:
        return 1.0, "The level has no collectibles."
    return 1.0, f"Bots collected every collectible ({total} in all)."


def _performance(telemetry: TelemetrySummary, min_fps: float) -> _Measure:
    lows = [(bot.fps.min, bot.bot) for bot in telemetry.bots if bot.fps is not None]
    if not lows:
        return None, "No FPS samples."
    fps, bot = min(lows, key=itemgetter(0))
    return (
        1.0 if fps >= min_fps else 0.0,
        f"Minimum FPS {fps:.1f} ({bot}); at least {min_fps:g} required.",
    )


def _critic_rating(item_id: str, verified: VerifiedCritique | None) -> _Measure:
    if verified is None:
        return None, "No verified critique."
    rating = verified.rubric_assessment.get(item_id)
    if rating is None:
        return None, f"The critic did not rate {item_id!r}."
    return rating / _MAX_ASSESSMENT, f"The critic rated it {rating}/{_MAX_ASSESSMENT}."


def _weighted_mean(items: Sequence[ItemScore]) -> float:
    """Mean of the measured scores, weighted; 0 when nothing was measured."""
    measured = [(item.weight, item.score) for item in items if item.score is not None]
    total_weight = sum(weight for weight, _ in measured)
    if not total_weight:
        return 0.0
    return sum(weight * score for weight, score in measured) / total_weight


def _bot(telemetry: TelemetrySummary, name: BotName) -> BotTelemetrySummary | None:
    return next((bot for bot in telemetry.bots if bot.bot == name), None)


def _in_time(bot: BotTelemetrySummary) -> str:
    seconds = bot.time_to_complete_s
    return "" if seconds is None else f" in {seconds:.1f} s"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
