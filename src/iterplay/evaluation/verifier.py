"""[6] Verifier: checks every critic claim against telemetry (HLD §4 [6])."""

import itertools
import math
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from operator import itemgetter
from types import MappingProxyType
from typing import Final, TypeAlias

from iterplay.contract.state import KILL_PLANE_Y, PlayerState, Vec3
from iterplay.models.critique import Critique, EvidenceType, Issue, Severity
from iterplay.models.playtest import BotName, BotRunResult, PlaytestReport, RunEnd
from iterplay.models.rubric import RubricItemId
from iterplay.models.telemetry import (
    BotTelemetrySummary,
    StuckInterval,
    TelemetryRecord,
    TelemetrySummary,
)
from iterplay.models.verification import (
    IssueOrigin,
    VerificationStatus,
    VerifiedCritique,
    VerifiedIssue,
)

_Verdict: TypeAlias = tuple[VerificationStatus, str]

_RUBRIC_ITEM_NAMES: Final[Mapping[str, RubricItemId]] = MappingProxyType(
    {
        **{item.value: item for item in RubricItemId},
        "collision": RubricItemId.COLLISION_INTEGRITY,
        "collisions": RubricItemId.COLLISION_INTEGRITY,
        "difficulty": RubricItemId.FAIR_DIFFICULTY,
        "fps": RubricItemId.PERFORMANCE,
        "perf": RubricItemId.PERFORMANCE,
        "framerate": RubricItemId.PERFORMANCE,
        "frame_rate": RubricItemId.PERFORMANCE,
        "errors": RubricItemId.NO_RUNTIME_ERRORS,
        "crash": RubricItemId.NO_RUNTIME_ERRORS,
        "crashes": RubricItemId.NO_RUNTIME_ERRORS,
        "completion": RubricItemId.COMPLETABLE,
        "finishable": RubricItemId.COMPLETABLE,
        "collectibles": RubricItemId.COLLECTIBLES_REACHABLE,
    }
)
"""Rubric item ids, plus the other names critics give them, such as the HLD's "collisions"."""

_LISTED: Final = 3
"""Moments or ids spelled out in a rationale before the rest are only counted."""

_QUOTE_CHARS: Final = 200
"""Longest error message quoted in a rationale."""

_MESSAGE_CHARS: Final = 2000
"""Longest error message kept in an issue's observation; enough for a stack trace."""


@dataclass(frozen=True, slots=True)
class VerifierConfig:
    time_tolerance_s: float = 2.0
    """How far from a claim's timestamp corroborating telemetry may be."""

    kill_plane_y: float = KILL_PLANE_Y

    min_fps: float = 30.0
    """A stutter claim needs the FPS to drop below this near its timestamp."""

    stuck_issue_s: float = 5.0
    """Stuck intervals at least this long become deterministic issues."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Locally produced ground truth that claims are checked against."""

    playtest: PlaytestReport
    telemetry: Mapping[BotName, Sequence[TelemetryRecord]]
    summary: TelemetrySummary


@dataclass(frozen=True, slots=True)
class _Scope:
    """The telemetry a claim is about: one bot's run or the whole playtest, near a moment or
    throughout."""

    bot: BotName | None
    at: float | None
    tolerance_s: float

    @property
    def runs(self) -> str:
        return "the playtest" if self.bot is None else f"the {self.bot} run"

    def includes(self, bot: BotName, video_t: float) -> bool:
        return (self.bot is None or bot == self.bot) and (
            self.at is None or abs(video_t - self.at) <= self.tolerance_s
        )

    def moment(self, bot: BotName, video_t: float) -> str:
        """``video_t`` as mm:ss, naming the bot unless the claim already does."""
        return _mmss(video_t) if self.bot is not None else f"{bot} {_mmss(video_t)}"

    def __str__(self) -> str:
        if self.at is None:
            return self.runs
        return f"{self.runs} within {self.tolerance_s:g} s of {_mmss(self.at)}"


@dataclass(frozen=True, slots=True)
class _Finding:
    """A deterministic issue before it is numbered."""

    rubric_item: RubricItemId
    severity: Severity
    evidence_type: EvidenceType
    observation: str
    rationale: str
    fix_hint: str
    bot: BotName | None = None
    timestamp: str | None = None

    def verified(self, issue_id: str) -> VerifiedIssue:
        issue = Issue(
            id=issue_id,
            bot=self.bot,
            timestamp=self.timestamp,
            rubric_item=self.rubric_item.value,
            severity=self.severity,
            observation=self.observation,
            evidence_type=self.evidence_type,
            fix_hint=self.fix_hint,
        )
        return VerifiedIssue(
            issue=issue,
            origin=IssueOrigin.DETERMINISTIC,
            status=VerificationStatus.CONFIRMED,
            rationale=self.rationale,
        )


class Verifier:
    """Deterministic, local check of each critic claim before the Fixer sees it.

    Claims are routed by rubric item, given by id or a common alias such as ``collisions`` or
    ``fps``. A claim's ``mm:ss`` timestamp and bot narrow the telemetry it is checked against;
    without a bot, every run counts.

    - ``collision_integrity``: confirmed by an unintentional ``oob`` event, or by
      ``y < kill_plane_y`` that no ``oob`` event accounts for, for that bot within
      ``time_tolerance_s`` of the timestamp; otherwise contradicted. Falls into intentional
      pits alone leave the claim unverifiable, because the rubric item ignores them.
    - ``completable``: a "level impossible" claim is contradicted if any bot completed it,
      otherwise confirmed.
    - ``collectibles_reachable``: confirmed if some collectible was never collected,
      contradicted if every collectible was.
    - ``fair_difficulty``: confirmed by deaths near the timestamp, otherwise unverifiable.
    - ``performance``: confirmed by an FPS dip below ``min_fps`` near the timestamp,
      otherwise contradicted.
    - ``no_runtime_errors``: confirmed by a matching uncaught or console error (or a crash) in
      that bot's run, otherwise contradicted.
    - anything else (design-doc match, feel): unverifiable, kept at lower priority.

    A claim with no telemetry to check it against is unverifiable rather than contradicted.
    Contradicted claims are kept in the output, so the viewer can show them as caught
    hallucinations, but never reach the Fixer. The Verifier also adds deterministic issues
    the critic missed: uncaught JS errors, collectibles no bot collected, and bots stuck for
    ``stuck_issue_s`` or longer.
    """

    def __init__(self, config: VerifierConfig | None = None) -> None:
        self._config = config or VerifierConfig()

    def verify(self, critique: Critique, evidence: Evidence) -> VerifiedCritique:
        """Check every critic issue, then add the deterministic issues the critic missed.

        Critic issues keep their order and are followed by the deterministic ones, numbered
        ``D1``, ``D2``, ... The critique's feel notes and rubric assessment pass through as is.
        """
        checked = [self._verify_claim(issue, evidence) for issue in critique.issues]
        return VerifiedCritique(
            issues=[*checked, *self._deterministic_issues(evidence, critic=checked)],
            feel_notes=list(critique.feel_notes),
            rubric_assessment=dict(critique.rubric_assessment),
        )

    def _verify_claim(self, issue: Issue, evidence: Evidence) -> VerifiedIssue:
        status, rationale = self._check(issue, evidence)
        return VerifiedIssue(
            issue=issue, origin=IssueOrigin.CRITIC, status=status, rationale=rationale
        )

    def _check(self, issue: Issue, evidence: Evidence) -> tuple[VerificationStatus, str]:
        """Status and rationale for one critic claim."""
        scope = _Scope(
            bot=issue.bot,
            at=_parse_timestamp(issue.timestamp),
            tolerance_s=self._config.time_tolerance_s,
        )
        match _rubric_item(issue.rubric_item):
            case RubricItemId.COLLISION_INTEGRITY:
                return self._check_collision(scope, evidence)
            case RubricItemId.COMPLETABLE:
                return _check_completable(evidence.summary)
            case RubricItemId.COLLECTIBLES_REACHABLE:
                return _check_collectibles(evidence.summary)
            case RubricItemId.FAIR_DIFFICULTY:
                return _check_difficulty(scope, evidence.summary)
            case RubricItemId.PERFORMANCE:
                return self._check_performance(scope, evidence.telemetry)
            case RubricItemId.NO_RUNTIME_ERRORS:
                return _check_errors(scope, evidence.playtest)
            case _:
                return (
                    VerificationStatus.UNVERIFIABLE,
                    f"Telemetry cannot check {issue.rubric_item!r} claims; kept at lower priority.",
                )

    def _check_collision(self, scope: _Scope, evidence: Evidence) -> _Verdict:
        falls = [
            (bot.bot, oob)
            for bot in evidence.summary.bots
            for oob in bot.out_of_bounds
            if scope.includes(bot.bot, oob.video_t)
        ]
        clips = [scope.moment(bot, oob.video_t) for bot, oob in falls if not oob.intentional]
        if clips:
            return (
                VerificationStatus.CONFIRMED,
                f"{_count(len(clips), 'unintentional oob event')} in {scope}: {_listing(clips)}.",
            )
        records = _records(scope, evidence.telemetry)
        if not records:
            return VerificationStatus.UNVERIFIABLE, f"No telemetry for {scope}."
        kill_plane = self._config.kill_plane_y
        reported = {bot for bot, _ in falls}
        unreported = [
            (bot, record)
            for bot, record in records
            if bot not in reported and record.state.player.y < kill_plane
        ]
        if unreported:
            bot, record = unreported[0]
            return (
                VerificationStatus.CONFIRMED,
                f"The player fell below the kill plane (y = {kill_plane:g}) without an oob event "
                f"in {scope}, reaching y = {record.state.player.y:.1f} at "
                f"{scope.moment(bot, record.video_t)}.",
            )
        if falls:
            pits = [scope.moment(bot, oob.video_t) for bot, oob in falls]
            return (
                VerificationStatus.UNVERIFIABLE,
                f"Only falls into intentional pits in {scope}: {_listing(pits)}. The game "
                "reports them as designed.",
            )
        return (
            VerificationStatus.CONTRADICTED,
            f"No oob event or fall below y = {kill_plane:g} in {scope}.",
        )

    def _check_performance(
        self, scope: _Scope, telemetry: Mapping[BotName, Sequence[TelemetryRecord]]
    ) -> _Verdict:
        samples = [
            (bot, record)
            for bot, record in _records(scope, telemetry)
            if math.isfinite(record.state.fps)
        ]
        if not samples:
            return VerificationStatus.UNVERIFIABLE, f"No FPS samples for {scope}."
        bot, slowest = min(samples, key=lambda sample: sample[1].state.fps)
        fps, min_fps = slowest.state.fps, self._config.min_fps
        if fps < min_fps:
            return (
                VerificationStatus.CONFIRMED,
                f"FPS fell to {fps:.1f}, below {min_fps:g}, at "
                f"{scope.moment(bot, slowest.video_t)} in {scope}.",
            )
        return (
            VerificationStatus.CONTRADICTED,
            f"FPS stayed at or above {min_fps:g} in {scope}; the lowest was {fps:.1f}.",
        )

    def _deterministic_issues(
        self, evidence: Evidence, *, critic: Sequence[VerifiedIssue] = ()
    ) -> list[VerifiedIssue]:
        """Issues the critic missed, numbered ``D1``, ``D2``, ... around the critic's own ids.

        ``critic`` holds the checked critic issues. A confirmed critic claim that the level
        cannot be completed makes the deterministic completion issue redundant.
        """
        findings = [
            *_error_findings(evidence.playtest),
            *_collectible_findings(evidence.summary),
            *self._stuck_findings(evidence.summary),
            *_completion_findings(evidence, critic),
        ]
        ids = _fresh_ids("D", taken={verified.issue.id for verified in critic})
        return [finding.verified(next(ids)) for finding in findings]

    def _stuck_findings(self, summary: TelemetrySummary) -> list[_Finding]:
        return [
            _stuck_finding(bot, interval)
            for bot in summary.bots
            for interval in bot.stuck_intervals
            if interval.end_video_t - interval.start_video_t >= self._config.stuck_issue_s
        ]


def _rubric_item(name: str) -> RubricItemId | None:
    """The rubric item a claim names, tolerating case, spaces, hyphens and aliases."""
    return _RUBRIC_ITEM_NAMES.get(name.strip().lower().replace("-", "_").replace(" ", "_"))


def _check_completable(summary: TelemetrySummary) -> _Verdict:
    if not summary.bots:
        return VerificationStatus.UNVERIFIABLE, "No telemetry to check completion against."
    finishers = [bot for bot in summary.bots if bot.completed]
    if not finishers:
        return (
            VerificationStatus.CONFIRMED,
            f"No bot completed the level in {_count(len(summary.bots), 'run')}.",
        )
    times = [bot.time_to_complete_s for bot in finishers if bot.time_to_complete_s is not None]
    fastest = f", the fastest in {min(times):.1f} s of game time" if times else ""
    return (
        VerificationStatus.CONTRADICTED,
        f"Completed by {', '.join(bot.bot for bot in finishers)}{fastest}.",
    )


def _check_collectibles(summary: TelemetrySummary) -> _Verdict:
    if not summary.bots:
        return VerificationStatus.UNVERIFIABLE, "No telemetry to check collectibles against."
    missing = summary.never_collected_ids
    total = len(_collectible_ids(summary))
    if missing:
        return (
            VerificationStatus.CONFIRMED,
            f"{len(missing)} of {_count(total, 'collectible')} never collected: "
            f"{_listing(missing)}.",
        )
    if total == 0:
        return VerificationStatus.CONTRADICTED, "The level has no collectibles."
    return VerificationStatus.CONTRADICTED, f"Every collectible was collected ({total} in all)."


def _check_difficulty(scope: _Scope, summary: TelemetrySummary) -> _Verdict:
    deaths = [
        scope.moment(bot.bot, death.video_t)
        for bot in summary.bots
        for death in bot.deaths
        if scope.includes(bot.bot, death.video_t)
    ]
    if deaths:
        return (
            VerificationStatus.CONFIRMED,
            f"{_count(len(deaths), 'death')} in {scope}: {_listing(deaths)}.",
        )
    return (
        VerificationStatus.UNVERIFIABLE,
        f"No deaths in {scope}, and telemetry cannot judge difficulty otherwise.",
    )


def _check_errors(scope: _Scope, playtest: PlaytestReport) -> _Verdict:
    """Errors carry no timestamps, so only the claim's bot narrows the search."""
    runs = [run for run in playtest.runs if scope.bot is None or run.bot == scope.bot]
    if not runs:
        missing = "runs" if scope.bot is None else f"{scope.bot} run"
        return VerificationStatus.UNVERIFIABLE, f"The playtest has no {missing} to check."
    errors = [error for run in runs for error in _run_errors(run)]
    if errors:
        return (
            VerificationStatus.CONFIRMED,
            f"{_count(len(errors), 'error')} logged in {scope.runs}, the first: "
            f"{_truncate(errors[0], _QUOTE_CHARS)!r}.",
        )
    return VerificationStatus.CONTRADICTED, f"No uncaught or console errors in {scope.runs}."


def _run_errors(run: BotRunResult) -> list[str]:
    """Uncaught and console errors, plus the crash itself if the session crashed."""
    crash = [run.crash_detail or "The session crashed."] if run.ended_by is RunEnd.CRASHED else []
    return [*run.uncaught_errors, *run.console_errors, *crash]


def _error_findings(playtest: PlaytestReport) -> list[_Finding]:
    """One per distinct uncaught error message, in order of first appearance."""
    bots_by_message: dict[str, list[BotName]] = {}
    for run in playtest.runs:
        for message in run.uncaught_errors:
            bots_by_message.setdefault(message.strip(), []).append(run.bot)
    return [_error_finding(message, bots) for message, bots in bots_by_message.items()]


def _error_finding(message: str, occurrences: Sequence[BotName]) -> _Finding:
    runs = list(dict.fromkeys(occurrences))
    return _Finding(
        rubric_item=RubricItemId.NO_RUNTIME_ERRORS,
        severity=Severity.CRITICAL,
        evidence_type=EvidenceType.CONSOLE,
        bot=runs[0] if len(runs) == 1 else None,
        observation=f"Uncaught error: {_truncate(message, _MESSAGE_CHARS) or '(no message)'}",
        rationale=(
            f"Thrown uncaught {_count(len(occurrences), 'time')} across "
            f"{_count(len(runs), 'run')} ({', '.join(runs)})."
        ),
        fix_hint="Fix the code that throws this error, or guard against the case behind it.",
    )


def _collectible_findings(summary: TelemetrySummary) -> list[_Finding]:
    missing = summary.never_collected_ids
    if not missing:
        return []
    total = len(_collectible_ids(summary))
    return [
        _Finding(
            rubric_item=RubricItemId.COLLECTIBLES_REACHABLE,
            severity=Severity.HIGH,
            evidence_type=EvidenceType.TELEMETRY,
            observation=(
                f"No bot collected {len(missing)} of the {_count(total, 'collectible')}: "
                f"{', '.join(missing)}."
            ),
            rationale=(
                "No collect event or collected flag for them in "
                f"{_count(len(summary.bots), 'run')}."
            ),
            fix_hint="Move these collectibles within reach of the platforms, or add a way up.",
        )
    ]


def _stuck_finding(bot: BotTelemetrySummary, interval: StuckInterval) -> _Finding:
    start, end = _mmss(interval.start_video_t), _mmss(interval.end_video_t)
    duration = interval.end_video_t - interval.start_video_t
    outcome = "still completed" if bot.completed else "never completed"
    return _Finding(
        rubric_item=RubricItemId.FAIR_DIFFICULTY if bot.completed else RubricItemId.COMPLETABLE,
        severity=Severity.MEDIUM,
        evidence_type=EvidenceType.TELEMETRY,
        bot=bot.bot,
        timestamp=start,
        observation=(
            f"The {bot.bot} bot was stuck near {_format_position(interval.position)} for "
            f"{duration:.1f} s, from {start} to {end}."
        ),
        rationale=(
            f"The player barely moved for {duration:.1f} s while the level was incomplete; "
            f"the {bot.bot} bot {outcome} the level."
        ),
        fix_hint=(
            "Check the level around this spot for snags, walls too high to jump, or gaps too "
            "wide to clear."
        ),
    )


def _completion_findings(evidence: Evidence, critic: Sequence[VerifiedIssue]) -> list[_Finding]:
    summary = evidence.summary
    if not summary.bots or any(bot.completed for bot in summary.bots):
        return []
    if any(
        verified.status is VerificationStatus.CONFIRMED
        and _rubric_item(verified.issue.rubric_item) is RubricItemId.COMPLETABLE
        for verified in critic
    ):
        return []
    return [
        _Finding(
            rubric_item=RubricItemId.COMPLETABLE,
            severity=Severity.CRITICAL,
            evidence_type=EvidenceType.TELEMETRY,
            observation=f"No bot completed the level.{_closest_approach(evidence.telemetry)}",
            rationale=(
                f"levelComplete stayed false throughout {_count(len(summary.bots), 'run')}."
            ),
            fix_hint=(
                "Make sure the goal can be reached, and that reaching it sets levelComplete "
                "to true."
            ),
        )
    ]


def _closest_approach(telemetry: Mapping[BotName, Sequence[TelemetryRecord]]) -> str:
    """A sentence on how close any bot came to the goal, or "" without telemetry."""
    approaches = [
        (_goal_distance(record), bot, record)
        for bot, records in telemetry.items()
        for record in records
    ]
    finite = [approach for approach in approaches if math.isfinite(approach[0])]
    if not finite:
        return ""
    distance, bot, record = min(finite, key=itemgetter(0))
    return (
        f" The closest any bot came to the goal was {distance:.1f} units, at "
        f"{_format_position(record.state.player)} ({bot}, {_mmss(record.video_t)})."
    )


def _goal_distance(record: TelemetryRecord) -> float:
    player, goal = record.state.player, record.state.goal
    return math.dist((player.x, player.y, player.z), (goal.x, goal.y, goal.z))


def _records(
    scope: _Scope, telemetry: Mapping[BotName, Sequence[TelemetryRecord]]
) -> list[tuple[BotName, TelemetryRecord]]:
    return [
        (bot, record)
        for bot, records in telemetry.items()
        for record in records
        if scope.includes(bot, record.video_t)
    ]


def _collectible_ids(summary: TelemetrySummary) -> set[str]:
    """Every collectible the summary knows of, collected or not."""
    collected = (collectible_id for bot in summary.bots for collectible_id in bot.collected_ids)
    return {*summary.never_collected_ids, *collected}


def _fresh_ids(prefix: str, taken: Collection[str]) -> Iterator[str]:
    """``<prefix>1``, ``<prefix>2``, ..., skipping ids already in use."""
    for number in itertools.count(1):
        candidate = f"{prefix}{number}"
        if candidate not in taken:
            yield candidate


def _parse_timestamp(timestamp: str | None) -> float | None:
    """Seconds from a ``mm:ss`` timestamp."""
    if timestamp is None:
        return None
    minutes, seconds = timestamp.split(":")
    return float(int(minutes) * 60 + int(seconds))


def _mmss(seconds: float) -> str:
    """``seconds`` as the burned-in clock shows it: truncated to whole seconds."""
    whole = min(max(int(seconds), 0), 99 * 60 + 59)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _format_position(position: Vec3 | PlayerState) -> str:
    """``(x, y)``, with z only for positions off the 2D plane."""
    coordinates = [position.x, position.y] + ([position.z] if position.z else [])
    return "(" + ", ".join(f"{value:.1f}" for value in coordinates) + ")"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _listing(items: Sequence[str]) -> str:
    """The first few items, then how many more there are."""
    shown = ", ".join(items[:_LISTED])
    rest = len(items) - _LISTED
    return f"{shown} and {rest} more" if rest > 0 else shown


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
