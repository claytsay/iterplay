"""``DirectCalls`` critic backend: plain model calls, no agent framework (HLD §5)."""

import asyncio
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml

from iterplay.agents.critic.base import CriticBackend, CriticInput
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.errors import LLMError
from iterplay.llm.base import LLMClient, LLMRequest, MediaInput, parse_response
from iterplay.models.critique import Critique
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItem
from iterplay.models.telemetry import TelemetrySummary

VIDEO_MIME_TYPE: Final = "video/mp4"
"""Media prep writes every clip as H.264 MP4."""

PERSONAS: Final[Mapping[BotName, str]] = MappingProxyType(
    {
        BotName.RANDOM: (
            "It holds random combinations of keys for random lengths of time, without aiming "
            "anywhere. Its run finds crashes, falls through the floor or out of bounds, and "
            "spots where the player gets stuck in geometry. It is not trying to win, so do not "
            "judge the level's difficulty or goals by its progress."
        ),
        BotName.SPEEDRUNNER: (
            "It reads positions from the game state, runs greedily to the nearest uncollected "
            "collectible and then to the goal, and jumps when it stops making progress or "
            "walks off a ledge. Its run shows whether the level can be finished, which "
            "collectibles are unreachable, and where the difficulty spikes."
        ),
        BotName.NEWBIE: (
            "It plays like the speedrunner bot but with a first-time player's flaws: input "
            "lag, overshooting and random pauses. Its run shows unforgiving jumps, unclear "
            "goals and a frustrating difficulty curve."
        ),
    }
)
"""How each persona critic is told its bot plays (HLD §4 [3])."""

_CRITIC_INSTRUCTION: Final = (
    "You are an expert game designer reviewing playtest footage of a browser game. Respond "
    "only with JSON that matches the response schema."
)

_LEAD_DESIGNER_INSTRUCTION: Final = (
    "You are the lead designer of a browser game, merging playtest reports. Respond only "
    "with JSON that matches the response schema."
)

_LEAD_DESIGNER_PURPOSE: Final = "critic.lead_designer"

_MAX_CONSOLE_ERRORS: Final = 20
"""Distinct console errors listed in a prompt; the rest are only counted."""

_MAX_ERROR_CHARS: Final = 1000
"""Longest console error quoted in a prompt."""

_MAX_CLOCK_S: Final = 99 * 60 + 59
"""The last second an mm:ss clock can show."""

_BACKTICK_RUN: Final = re.compile(r"`+")

_log = logging.getLogger(__name__)


class DirectCalls(CriticBackend):
    """The MVP critic.

    Single-pass mode sends the combined clip to one critic (HLD §4 [5]). Persona mode runs one
    critic per bot clip in parallel with ``asyncio.gather``, then a Lead Designer call merges,
    de-duplicates and prioritises their findings, keeping at most ``max_issues`` (HLD §5).

    Persona mode falls back to a single pass when there are no per-bot clips. A persona
    critic that fails with ``LLMError`` is left out with a warning, so long as another one
    succeeds. Issue ids are unique in every critique: when the model repeats an id or leaves
    one blank, the issues are renumbered ``I1``, ``I2``, ... in order.

    Raises:
        ValueError: ``max_issues`` is less than 1.
    """

    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptLibrary,
        *,
        per_persona: bool = False,
        max_issues: int = 5,
    ) -> None:
        if max_issues < 1:
            raise ValueError(f"max_issues must be at least 1, not {max_issues}")
        self._llm = llm
        self._prompts = prompts
        self._per_persona = per_persona
        self._max_issues = max_issues

    async def critique(self, request: CriticInput) -> Critique:
        if not self._per_persona:
            return await self._critique_combined(request)
        bots = [bot for bot in BotName if bot in request.media.per_bot]
        if not bots:
            _log.warning("No per-bot clips for the persona critics; critiquing in a single pass")
            return await self._critique_combined(request)
        return await self._lead_designer(request, await self._critique_personas(request, bots))

    async def _critique_combined(self, request: CriticInput) -> Critique:
        prompt = self._prompts.render(
            PromptName.CRITIC,
            design_doc=request.design_doc,
            rubric=_render_rubric(request.rubric),
            telemetry_summary=_render_telemetry(request.telemetry),
            console_errors=_render_console_errors(request.console_errors),
        )
        return await self._ask("critic", _CRITIC_INSTRUCTION, prompt, request.media.combined)

    async def _critique_personas(
        self, request: CriticInput, bots: Sequence[BotName]
    ) -> dict[BotName, Critique]:
        """Every bot's persona critique, run concurrently, in clip order.

        Raises:
            LLMError: Every persona critic failed; this is the first bot's error.
        """
        outcomes = await asyncio.gather(
            *(self._critique_persona(request, bot) for bot in bots), return_exceptions=True
        )
        critiques: dict[BotName, Critique] = {}
        failures: list[LLMError] = []
        for bot, outcome in zip(bots, outcomes, strict=True):
            if isinstance(outcome, Critique):
                critiques[bot] = outcome
            elif isinstance(outcome, LLMError):
                _log.warning("Leaving the %s persona critique out: %s", bot, outcome)
                failures.append(outcome)
            else:
                raise outcome
        if not critiques:
            raise failures[0]
        return critiques

    async def _critique_persona(self, request: CriticInput, bot: BotName) -> Critique:
        """Critique one bot's clip only, for deeper and more focused findings.

        Every issue is attributed to ``bot``, since its timestamps are on that clip's clock.
        """
        prompt = self._prompts.render(
            PromptName.PERSONA_CRITIC,
            bot=bot.value,
            persona=PERSONAS[bot],
            design_doc=request.design_doc,
            rubric=_render_rubric(request.rubric),
            telemetry_summary=_render_bot_telemetry(request.telemetry, bot),
            console_errors=_render_console_errors(request.console_errors),
        )
        critique = await self._ask(
            f"critic.{bot}", _CRITIC_INSTRUCTION, prompt, request.media.per_bot[bot]
        )
        issues = [issue.model_copy(update={"bot": bot}) for issue in critique.issues]
        return critique.model_copy(update={"issues": issues})

    async def _lead_designer(
        self, request: CriticInput, critiques: Mapping[BotName, Critique]
    ) -> Critique:
        """Merge persona critiques, resolving conflicts and applying the rubric.

        Only the first ``max_issues`` issues are kept should the model return more.
        """
        prompt = self._prompts.render(
            PromptName.LEAD_DESIGNER,
            max_issues=str(self._max_issues),
            rubric=_render_rubric(request.rubric),
            telemetry_summary=_render_telemetry(request.telemetry),
            critiques=_render_critiques(critiques),
        )
        merged = await self._ask(_LEAD_DESIGNER_PURPOSE, _LEAD_DESIGNER_INSTRUCTION, prompt)
        if len(merged.issues) <= self._max_issues:
            return merged
        _log.warning(
            "The lead designer kept %d issues; keeping the first %d",
            len(merged.issues),
            self._max_issues,
        )
        return merged.model_copy(update={"issues": merged.issues[: self._max_issues]})

    async def _ask(
        self, purpose: str, system_instruction: str, prompt: str, clip: Path | None = None
    ) -> Critique:
        request = LLMRequest(
            purpose=purpose,
            system_instruction=system_instruction,
            prompt=prompt,
            response_schema=Critique,
            media=() if clip is None else (MediaInput(path=clip, mime_type=VIDEO_MIME_TYPE),),
        )
        critique = parse_response(await self._llm.generate(request), Critique)
        return _with_unique_ids(critique, purpose)


def _with_unique_ids(critique: Critique, purpose: str) -> Critique:
    """``critique``, renumbered ``I1``, ``I2``, ... if an issue id repeats or is blank."""
    ids = [issue.id for issue in critique.issues]
    if len(set(ids)) == len(ids) and all(issue_id.strip() for issue_id in ids):
        return critique
    _log.warning("The %s response repeated or left out issue ids; renumbering them", purpose)
    issues = [
        issue.model_copy(update={"id": f"I{number}"})
        for number, issue in enumerate(critique.issues, start=1)
    ]
    return critique.model_copy(update={"issues": issues})


def _render_rubric(rubric: Rubric) -> str:
    """Every item's id, source, weight, description and params, as YAML."""
    items = [_rubric_fields(item) for item in rubric.items]
    text = yaml.safe_dump(items, sort_keys=False, allow_unicode=True, width=math.inf)
    return _fenced(text, "yaml")


def _rubric_fields(item: RubricItem) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": item.id,
        "source": item.source.value,
        "weight": _compact(item.weight),
        "description": item.description,
    }
    if item.params:
        fields["params"] = {name: _compact(value) for name, value in item.params.items()}
    return fields


def _compact(value: float) -> int | float:
    """``15.0`` as ``15``, so whole numbers read as such."""
    return int(value) if float(value).is_integer() else value


def _render_telemetry(summary: TelemetrySummary) -> str:
    return _json_block(summary.model_dump(mode="json"))


def _render_bot_telemetry(summary: TelemetrySummary, bot: BotName) -> str:
    own = next((entry for entry in summary.bots if entry.bot is bot), None)
    if own is None:
        return "No telemetry was recorded for this run."
    return _json_block(own.model_dump(mode="json"))


def _render_critiques(critiques: Mapping[BotName, Critique]) -> str:
    """Each persona critique, keyed by bot name."""
    return _json_block(
        {bot.value: critique.model_dump(mode="json") for bot, critique in critiques.items()}
    )


def _json_block(data: object) -> str:
    return _fenced(json.dumps(_with_clock_times(data), indent=2, ensure_ascii=False), "json")


def _with_clock_times(value: object) -> object:
    """JSON data with an ``mm:ss`` field after every ``*video_t`` field.

    ``video_t`` becomes ``mm:ss``, and ``start_video_t`` becomes ``start_mm:ss``.
    """
    if isinstance(value, list):
        return [_with_clock_times(item) for item in value]
    if not isinstance(value, dict):
        return value
    annotated: dict[str, object] = {}
    for key, item in value.items():
        annotated[key] = _with_clock_times(item)
        if key.endswith("video_t") and isinstance(item, int | float) and not isinstance(item, bool):
            annotated[key.removesuffix("video_t") + "mm:ss"] = _clock(item)
    return annotated


def _clock(seconds: float) -> str:
    """``seconds`` as the burned-in clock shows it: whole seconds, as mm:ss."""
    whole = int(min(max(seconds, 0.0), _MAX_CLOCK_S))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _render_console_errors(errors: Sequence[str]) -> str:
    """Distinct errors in first-seen order as a bulleted list, or "None."."""
    counts = Counter(error.strip() for error in errors if error.strip())
    if not counts:
        return "None."
    listed = list(counts.items())[:_MAX_CONSOLE_ERRORS]
    lines = [_console_error_bullet(error, count) for error, count in listed]
    if len(counts) > len(listed):
        lines.append(f"- ...and {len(counts) - len(listed)} more distinct errors.")
    return "\n".join(lines)


def _console_error_bullet(error: str, count: int) -> str:
    if len(error) > _MAX_ERROR_CHARS:
        error = error[: _MAX_ERROR_CHARS - 3] + "..."
    repeats = f" (logged {count} times)" if count > 1 else ""
    return "- " + "\n  ".join(error.splitlines()) + repeats


def _fenced(text: str, language: str) -> str:
    """``text`` in a code fence longer than any run of backticks inside it."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text if text.endswith("\n") else f"{text}\n"
    return f"{fence}{language}\n{body}{fence}"
