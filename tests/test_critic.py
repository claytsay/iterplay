"""The critic backends turn footage, telemetry and the rubric into a raw critique
(HLD §4 [5], §5)."""

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from iterplay.agents.critic.base import CriticInput
from iterplay.agents.critic.direct import PERSONAS, DirectCalls
from iterplay.agents.critic.managed import ManagedAgents
from iterplay.agents.prompt_library import PromptLibrary, PromptName
from iterplay.contract.state import Vec3
from iterplay.contract.templates import TEMPLATES, TemplateId
from iterplay.errors import BudgetExceededError, ConfigError, LLMError, LLMResponseError
from iterplay.llm.base import LLMRequest, LLMResponse, MediaInput
from iterplay.models.critique import Critique, Issue
from iterplay.models.media import PreparedMedia
from iterplay.models.playtest import BotName
from iterplay.models.rubric import Rubric, RubricItem, RubricSource
from iterplay.models.telemetry import (
    BotTelemetrySummary,
    FpsStats,
    OutOfBounds,
    StuckInterval,
    TelemetrySummary,
    TimedPosition,
)
from iterplay.settings import Settings
from tests.helpers import FakeLLMClient, Reply

RANDOM, SPEEDRUNNER, NEWBIE = BotName.RANDOM, BotName.SPEEDRUNNER, BotName.NEWBIE
LEAD_DESIGNER = "critic.lead_designer"

PLACEHOLDER = re.compile(r"\$\{?(?:design_doc|rubric|telemetry_summary|console_errors)\b")

DESIGN_DOC = "# Gem Dash\nCollect three gems, then touch the flag. Each gem is worth $5."

RUBRIC = Rubric(
    target=0.9,
    items=[
        RubricItem(
            id="completable",
            description="The Speedrunner completes the level.",
            source=RubricSource.TELEMETRY,
            weight=20,
        ),
        RubricItem(
            id="fair_difficulty",
            description="The Newbie dies at most max_deaths times in 60 s.",
            source=RubricSource.TELEMETRY,
            weight=10,
            params={"max_deaths": 3, "window_s": 60.5},
        ),
        RubricItem(
            id="game_feel",
            description="The critic's 0-5 rating of game feel.",
            source=RubricSource.CRITIC,
            weight=2.5,
        ),
    ],
)

RUBRIC_YAML = [
    {
        "id": "completable",
        "source": "telemetry",
        "weight": 20,
        "description": "The Speedrunner completes the level.",
    },
    {
        "id": "fair_difficulty",
        "source": "telemetry",
        "weight": 10,
        "description": "The Newbie dies at most max_deaths times in 60 s.",
        "params": {"max_deaths": 3, "window_s": 60.5},
    },
    {
        "id": "game_feel",
        "source": "critic",
        "weight": 2.5,
        "description": "The critic's 0-5 rating of game feel.",
    },
]


def bot_summary(bot: BotName, *, death_at: float = 42.7) -> BotTelemetrySummary:
    return BotTelemetrySummary(
        bot=bot,
        completed=bot is SPEEDRUNNER,
        time_to_complete_s=31.25 if bot is SPEEDRUNNER else None,
        deaths=[TimedPosition(video_t=death_at, position=Vec3(x=31.5, y=-20.5))],
        out_of_bounds=[
            OutOfBounds(video_t=death_at, position=Vec3(x=31.5, y=-20.5), intentional=False)
        ],
        stuck_intervals=[
            StuckInterval(start_video_t=9.95, end_video_t=75.0, position=Vec3(x=12, y=2))
        ],
        fps=FpsStats(min=24.5, median=58),
        collected_ids=["gem-1"],
    )


TELEMETRY = TelemetrySummary(
    bots=[bot_summary(RANDOM, death_at=3.2), bot_summary(SPEEDRUNNER), bot_summary(NEWBIE)],
    never_collected_ids=["gem-3"],
)


@pytest.fixture
def prompts() -> PromptLibrary:
    return PromptLibrary(TEMPLATES[TemplateId.PLATFORMER_2D])


def media(directory: Path, bots: Sequence[BotName] = tuple(BotName)) -> PreparedMedia:
    return PreparedMedia(
        combined=directory / "combined_360p.mp4",
        per_bot={bot: directory / f"{bot}_360p.mp4" for bot in bots},
    )


def critic_input(
    directory: Path,
    *,
    bots: Sequence[BotName] = tuple(BotName),
    console_errors: Sequence[str] = (),
    telemetry: TelemetrySummary = TELEMETRY,
) -> CriticInput:
    return CriticInput(
        design_doc=DESIGN_DOC,
        rubric=RUBRIC,
        media=media(directory, bots),
        telemetry=telemetry,
        console_errors=tuple(console_errors),
    )


def issue(issue_id: str, **fields: Any) -> Issue:
    return Issue.model_validate(
        {
            "id": issue_id,
            "rubric_item": "collision_integrity",
            "severity": "high",
            "observation": f"Observation {issue_id}.",
            "evidence_type": "visual",
            **fields,
        }
    )


def critique(*ids: str, **fields: Any) -> Critique:
    return Critique(
        issues=[issue(issue_id, **fields) for issue_id in ids],
        feel_notes=["Jumps feel floaty."],
        rubric_assessment={"game_feel": 3},
    )


def persona_replies(**overrides: Reply) -> dict[str, list[Reply]]:
    """A reply per persona critic and the Lead Designer, with some of them replaced."""
    replies: dict[str, Reply] = {
        f"critic.{bot}": critique("I1", "I2", bot=bot, timestamp="00:03") for bot in BotName
    }
    replies[LEAD_DESIGNER] = critique("I1", "I2", "I3", bot=SPEEDRUNNER, timestamp="00:42")
    replies.update({f"critic.{bot}": reply for bot, reply in overrides.items()})
    return {purpose: [reply] for purpose, reply in replies.items()}


def section(prompt: str, heading: str) -> str:
    """The text under ``## <heading>``, up to the next level-2 heading."""
    start = prompt.index(f"\n## {heading}\n") + len(heading) + 5
    end = prompt.find("\n## ", start)
    return prompt[start : None if end == -1 else end].strip()


def code_block(text: str, language: str) -> str:
    match = re.search(rf"^```{language}\n(.*?)\n```$", text, re.DOTALL | re.MULTILINE)
    assert match is not None, text
    return match.group(1)


def json_section(prompt: str, heading: str) -> Any:
    return json.loads(code_block(section(prompt, heading), "json"))


def without_clock_times(value: Any) -> Any:
    """``value`` with every ``mm:ss`` field the critic prompts add taken out again."""
    if isinstance(value, list):
        return [without_clock_times(item) for item in value]
    if isinstance(value, dict):
        return {
            key: without_clock_times(item)
            for key, item in value.items()
            if not key.endswith("mm:ss")
        }
    return value


async def single_pass_request(
    prompts: PromptLibrary, request: CriticInput, *replies: Reply
) -> tuple[LLMRequest, Critique]:
    llm = FakeLLMClient({"critic": list(replies) or [critique("I1")]})

    result = await DirectCalls(llm, prompts).critique(request)

    [sent] = llm.requests
    return sent, result


class GatedLLMClient(FakeLLMClient):
    """Holds every persona request until all of them are in flight at once.

    Run one at a time, the first request would wait in vain and time out.
    """

    def __init__(self, replies: Mapping[str, Sequence[Reply]], *, persona_count: int) -> None:
        super().__init__(replies)
        self._persona_count = persona_count
        self._in_flight = 0
        self._all_in_flight = asyncio.Event()
        self.events: list[str] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if request.purpose != LEAD_DESIGNER:
            self.events.append(f"start {request.purpose}")
            self._in_flight += 1
            if self._in_flight == self._persona_count:
                self._all_in_flight.set()
            await asyncio.wait_for(self._all_in_flight.wait(), timeout=5)
        response = await super().generate(request)
        self.events.append(f"end {request.purpose}")
        return response


# Single pass


async def test_single_pass_sends_the_combined_clip_to_one_critic(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    sent, _ = await single_pass_request(prompts, critic_input(tmp_path))

    assert sent.purpose == "critic"
    assert sent.response_schema is Critique
    assert sent.media == (MediaInput(path=tmp_path / "combined_360p.mp4", mime_type="video/mp4"),)
    assert "playtest footage" in sent.system_instruction
    assert "Respond only with JSON" in sent.system_instruction


async def test_the_critic_prompt_fills_every_placeholder(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    sent, _ = await single_pass_request(prompts, critic_input(tmp_path, console_errors=["Oops"]))
    rubric = section(sent.prompt, "Rubric").split("\n\n", 1)[1]
    telemetry = section(sent.prompt, "Telemetry summary").split("\n\n", 1)[1]

    assert sent.prompt == prompts.render(
        PromptName.CRITIC,
        design_doc=DESIGN_DOC,
        rubric=rubric,
        telemetry_summary=telemetry,
        console_errors="- Oops",
    )
    assert not PLACEHOLDER.search(sent.prompt)


async def test_the_rubric_lists_every_item_as_yaml(prompts: PromptLibrary, tmp_path: Path) -> None:
    sent, _ = await single_pass_request(prompts, critic_input(tmp_path))
    text = code_block(section(sent.prompt, "Rubric"), "yaml")

    assert yaml.safe_load(text) == RUBRIC_YAML
    assert "  weight: 20\n" in text
    assert "    max_deaths: 3\n" in text


async def test_the_telemetry_summary_adds_mm_ss_beside_every_video_t(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    sent, _ = await single_pass_request(prompts, critic_input(tmp_path))
    summary = json_section(sent.prompt, "Telemetry summary")
    speedrunner = summary["bots"][1]

    assert without_clock_times(summary) == TELEMETRY.model_dump(mode="json")
    assert list(speedrunner["deaths"][0]) == ["video_t", "mm:ss", "position"]
    assert speedrunner["deaths"][0]["mm:ss"] == "00:42"
    assert speedrunner["out_of_bounds"][0]["mm:ss"] == "00:42"
    assert list(speedrunner["stuck_intervals"][0]) == [
        "start_video_t",
        "start_mm:ss",
        "end_video_t",
        "end_mm:ss",
        "position",
    ]
    assert speedrunner["stuck_intervals"][0]["start_mm:ss"] == "00:09"
    assert speedrunner["stuck_intervals"][0]["end_mm:ss"] == "01:15"
    assert summary["bots"][0]["deaths"][0]["mm:ss"] == "00:03"


async def test_console_errors_are_a_bulleted_list_of_distinct_errors(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    errors = [
        "TypeError: player is undefined\n    at step (main.js:12)",
        "  ",
        "Failed to load resource: ./vendor/missing.js",
        "TypeError: player is undefined\n    at step (main.js:12)",
    ]

    sent, _ = await single_pass_request(prompts, critic_input(tmp_path, console_errors=errors))

    assert section(sent.prompt, "Console errors") == (
        "- TypeError: player is undefined\n"
        "      at step (main.js:12) (logged 2 times)\n"
        "- Failed to load resource: ./vendor/missing.js"
    )


async def test_no_console_errors_says_none(prompts: PromptLibrary, tmp_path: Path) -> None:
    sent, _ = await single_pass_request(prompts, critic_input(tmp_path))

    assert section(sent.prompt, "Console errors") == "None."


async def test_long_and_numerous_console_errors_are_cut_short(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    errors = ["x" * 5000, *(f"Error {number}" for number in range(30))]

    sent, _ = await single_pass_request(prompts, critic_input(tmp_path, console_errors=errors))

    lines = section(sent.prompt, "Console errors").splitlines()
    assert lines[0] == "- " + "x" * 997 + "..."
    assert lines[1:3] == ["- Error 0", "- Error 1"]
    assert lines[-2:] == ["- Error 18", "- ...and 11 more distinct errors."]
    assert len(lines) == 21


async def test_single_pass_returns_the_parsed_critique(
    prompts: PromptLibrary, tmp_path: Path, hld_critique: dict[str, Any]
) -> None:
    _, result = await single_pass_request(prompts, critic_input(tmp_path), json.dumps(hld_critique))

    assert result == Critique.model_validate(hld_critique)


@pytest.mark.parametrize("ids", [["I1", "I1", "I2"], ["I3", "", "I4"], ["A", " ", "B"]])
async def test_repeated_or_blank_ids_are_renumbered_in_order(
    prompts: PromptLibrary, tmp_path: Path, ids: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    reply = critique(*ids)

    _, result = await single_pass_request(prompts, critic_input(tmp_path), reply)

    assert [found.id for found in result.issues] == ["I1", "I2", "I3"]
    assert [found.observation for found in result.issues] == [
        found.observation for found in reply.issues
    ]
    assert result.feel_notes == reply.feel_notes
    assert result.rubric_assessment == reply.rubric_assessment
    assert "The critic response repeated or left out issue ids; renumbering them" in caplog.text


async def test_unique_ids_are_kept(prompts: PromptLibrary, tmp_path: Path) -> None:
    reply = critique("I3", "I7", "x-1")

    _, result = await single_pass_request(prompts, critic_input(tmp_path), reply)

    assert result == reply


async def test_invalid_critic_json_raises_without_a_retry(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    bad = json.dumps({"issues": [{**issue("I1").model_dump(mode="json"), "timestamp": "0:42"}]})
    llm = FakeLLMClient({"critic": [bad, critique("I1")]})

    with pytest.raises(LLMResponseError, match=r"issues\[0\]\.timestamp"):
        await DirectCalls(llm, prompts).critique(critic_input(tmp_path))

    assert len(llm.requests) == 1


@pytest.mark.parametrize("error", [LLMError("upload failed"), BudgetExceededError("spent")])
async def test_a_failed_critic_call_propagates(
    prompts: PromptLibrary, tmp_path: Path, error: Exception
) -> None:
    llm = FakeLLMClient({"critic": [error]})

    with pytest.raises(type(error)):
        await DirectCalls(llm, prompts).critique(critic_input(tmp_path))


# Persona mode


async def test_persona_mode_critiques_each_clip_then_asks_the_lead_designer(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies())

    result = await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    assert [sent.purpose for sent in llm.requests] == [
        "critic.random",
        "critic.speedrunner",
        "critic.newbie",
        LEAD_DESIGNER,
    ]
    assert result == critique("I1", "I2", "I3", bot=SPEEDRUNNER, timestamp="00:42")


async def test_each_persona_critic_gets_its_own_clip_persona_and_telemetry(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies())

    await DirectCalls(llm, prompts, per_persona=True).critique(
        critic_input(tmp_path, console_errors=["Oops"])
    )

    for bot, sent in zip(BotName, llm.requests[: len(BotName)], strict=True):
        own = next(summary for summary in TELEMETRY.bots if summary.bot is bot)
        telemetry = section(sent.prompt, "Telemetry summary for this run").split("\n\n", 1)[1]
        assert sent.media == (MediaInput(path=tmp_path / f"{bot}_360p.mp4", mime_type="video/mp4"),)
        assert sent.response_schema is Critique
        assert sent.prompt == prompts.render(
            PromptName.PERSONA_CRITIC,
            bot=bot.value,
            persona=PERSONAS[bot],
            design_doc=DESIGN_DOC,
            rubric=section(sent.prompt, "Rubric").split("\n\n", 1)[1],
            telemetry_summary=telemetry,
            console_errors="- Oops",
        )
        assert without_clock_times(json.loads(code_block(telemetry, "json"))) == own.model_dump(
            mode="json"
        )
        assert yaml.safe_load(code_block(section(sent.prompt, "Rubric"), "yaml")) == RUBRIC_YAML


def test_every_bot_has_a_persona() -> None:
    assert set(PERSONAS) == set(BotName)
    assert len(set(PERSONAS.values())) == len(BotName)


async def test_the_lead_designer_merges_the_persona_critiques(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies())

    await DirectCalls(llm, prompts, per_persona=True, max_issues=3).critique(critic_input(tmp_path))

    lead = llm.requests[-1]
    critiques = section(lead.prompt, "Critiques").split("\n\n", 1)[1]
    assert lead.media == ()
    assert lead.response_schema is Critique
    assert "lead designer" in lead.system_instruction
    assert lead.prompt == prompts.render(
        PromptName.LEAD_DESIGNER,
        max_issues="3",
        rubric=section(lead.prompt, "Rubric"),
        telemetry_summary=section(lead.prompt, "Telemetry summary").split("\n\n", 1)[1],
        critiques=critiques,
    )
    assert "Keep at most 3 issues" in lead.prompt
    assert yaml.safe_load(code_block(section(lead.prompt, "Rubric"), "yaml")) == RUBRIC_YAML
    full = json_section(lead.prompt, "Telemetry summary")
    assert without_clock_times(full) == TELEMETRY.model_dump(mode="json")
    assert full["bots"][1]["deaths"][0]["mm:ss"] == "00:42"
    assert json.loads(code_block(critiques, "json")) == {
        bot.value: critique("I1", "I2", bot=bot, timestamp="00:03").model_dump(mode="json")
        for bot in BotName
    }


async def test_persona_critics_run_concurrently(prompts: PromptLibrary, tmp_path: Path) -> None:
    llm = GatedLLMClient(persona_replies(), persona_count=len(BotName))

    result = await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    persona_events = llm.events[: 2 * len(BotName)]
    assert persona_events[: len(BotName)] == [f"start critic.{bot}" for bot in BotName]
    assert sorted(persona_events[len(BotName) :]) == sorted(f"end critic.{bot}" for bot in BotName)
    assert llm.events[-1] == f"end {LEAD_DESIGNER}"
    assert len(result.issues) == 3


async def test_persona_issues_are_attributed_to_their_bot(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    replies = persona_replies(newbie=critique("I1", "I2", bot=RANDOM), random=critique("I1"))
    llm = FakeLLMClient(replies)

    await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    critiques = json_section(llm.requests[-1].prompt, "Critiques")
    assert [found["bot"] for found in critiques["newbie"]["issues"]] == ["newbie", "newbie"]
    assert [found["bot"] for found in critiques["random"]["issues"]] == ["random"]


async def test_persona_ids_are_made_unique_before_merging(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies(speedrunner=critique("I4", "I4")))

    await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    critiques = json_section(llm.requests[-1].prompt, "Critiques")
    assert [found["id"] for found in critiques["speedrunner"]["issues"]] == ["I1", "I2"]


async def test_the_lead_designer_keeps_at_most_max_issues(
    prompts: PromptLibrary, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    replies = persona_replies()
    replies[LEAD_DESIGNER] = [critique("I1", "I2", "I3", "I4")]
    llm = FakeLLMClient(replies)

    result = await DirectCalls(llm, prompts, per_persona=True, max_issues=2).critique(
        critic_input(tmp_path)
    )

    assert [found.id for found in result.issues] == ["I1", "I2"]
    assert result.rubric_assessment == {"game_feel": 3}
    assert "The lead designer kept 4 issues; keeping the first 2" in caplog.text


async def test_the_lead_designer_ids_are_made_unique(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    replies = persona_replies()
    replies[LEAD_DESIGNER] = [critique("I1", "I1", "I2")]
    llm = FakeLLMClient(replies)

    result = await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    assert [found.id for found in result.issues] == ["I1", "I2", "I3"]


async def test_persona_mode_without_clips_falls_back_to_a_single_pass(
    prompts: PromptLibrary, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLMClient({"critic": [critique("I1")]})

    result = await DirectCalls(llm, prompts, per_persona=True).critique(
        critic_input(tmp_path, bots=())
    )

    [sent] = llm.requests
    assert sent.purpose == "critic"
    assert sent.media == (MediaInput(path=tmp_path / "combined_360p.mp4", mime_type="video/mp4"),)
    assert sent.prompt.startswith("You are the lead playtester")
    assert result == critique("I1")
    assert "No per-bot clips for the persona critics" in caplog.text


async def test_only_bots_with_a_clip_get_a_persona_critic(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies())

    await DirectCalls(llm, prompts, per_persona=True).critique(
        critic_input(tmp_path, bots=(NEWBIE, SPEEDRUNNER))
    )

    assert [sent.purpose for sent in llm.requests] == [
        "critic.speedrunner",
        "critic.newbie",
        LEAD_DESIGNER,
    ]
    assert list(json_section(llm.requests[-1].prompt, "Critiques")) == ["speedrunner", "newbie"]


async def test_a_bot_without_telemetry_is_told_so(prompts: PromptLibrary, tmp_path: Path) -> None:
    telemetry = TelemetrySummary(bots=[bot_summary(SPEEDRUNNER)], never_collected_ids=[])
    llm = FakeLLMClient(persona_replies())

    await DirectCalls(llm, prompts, per_persona=True).critique(
        critic_input(tmp_path, bots=(RANDOM,), telemetry=telemetry)
    )

    persona = llm.requests[0]
    assert section(persona.prompt, "Telemetry summary for this run").endswith(
        "\n\nNo telemetry was recorded for this run."
    )


async def test_a_failed_persona_critic_is_left_out(
    prompts: PromptLibrary, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLMClient(persona_replies(random=LLMError("upload failed"), newbie="not json"))

    result = await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    assert list(json_section(llm.requests[-1].prompt, "Critiques")) == ["speedrunner"]
    assert len(result.issues) == 3
    assert "Leaving the random persona critique out: upload failed" in caplog.text
    assert "Leaving the newbie persona critique out: Response does not match" in caplog.text


async def test_every_persona_critic_failing_raises_the_first_error(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    replies = persona_replies(
        random=LLMError("random failed"),
        speedrunner=LLMError("speedrunner failed"),
        newbie=LLMError("newbie failed"),
    )
    llm = FakeLLMClient(replies)

    with pytest.raises(LLMError, match=r"^random failed$"):
        await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    assert LEAD_DESIGNER not in [sent.purpose for sent in llm.requests]


async def test_a_budget_error_in_a_persona_critic_propagates(
    prompts: PromptLibrary, tmp_path: Path
) -> None:
    llm = FakeLLMClient(persona_replies(newbie=BudgetExceededError("Budget exhausted")))

    with pytest.raises(BudgetExceededError):
        await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))

    assert len(llm.requests) == len(BotName)


async def test_a_failed_lead_designer_propagates(prompts: PromptLibrary, tmp_path: Path) -> None:
    replies = persona_replies()
    replies[LEAD_DESIGNER] = ['{"issues": "none"}']
    llm = FakeLLMClient(replies)

    with pytest.raises(LLMResponseError, match=r"^Response does not match Critique"):
        await DirectCalls(llm, prompts, per_persona=True).critique(critic_input(tmp_path))


@pytest.mark.parametrize("max_issues", [0, -1])
def test_max_issues_must_be_positive(prompts: PromptLibrary, max_issues: int) -> None:
    with pytest.raises(ValueError, match="max_issues must be at least 1"):
        DirectCalls(FakeLLMClient(), prompts, max_issues=max_issues)


# Managed Agents


def test_the_managed_backend_is_not_available(prompts: PromptLibrary) -> None:
    with pytest.raises(ConfigError, match=r"not implemented yet.*--critic direct"):
        ManagedAgents(Settings(), prompts)


async def test_the_managed_backend_never_critiques(tmp_path: Path) -> None:
    backend = ManagedAgents.__new__(ManagedAgents)

    with pytest.raises(ConfigError, match="--critic direct"):
        await backend.critique(critic_input(tmp_path))
