"""Model-facing and on-disk schemas match HLD §4 and §6."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from iterplay.contract.state import GameState
from iterplay.models.critique import Critique, EvidenceType
from iterplay.models.game import BuilderResponse, FixerResponse
from iterplay.models.playtest import BotName
from iterplay.models.smoke import SmokeReport
from iterplay.models.telemetry import TelemetryRecord


def test_critique_accepts_hld_example(hld_critique: dict[str, Any]) -> None:
    issue = Critique.model_validate(hld_critique).issues[0]

    assert issue.bot is BotName.SPEEDRUNNER
    assert issue.evidence_type is EvidenceType.VISUAL_TELEMETRY


@pytest.mark.parametrize("score", [-1, 6])
def test_rubric_assessment_is_bounded(hld_critique: dict[str, Any], score: int) -> None:
    with pytest.raises(ValidationError):
        Critique.model_validate({**hld_critique, "rubric_assessment": {"feel": score}})


@pytest.mark.parametrize("timestamp", ["42", "0:42", "00:42.5"])
def test_issue_timestamp_must_be_mm_ss(hld_critique: dict[str, Any], timestamp: str) -> None:
    issue = {**hld_critique["issues"][0], "timestamp": timestamp}

    with pytest.raises(ValidationError):
        Critique.model_validate({**hld_critique, "issues": [issue]})


@pytest.mark.parametrize("schema", [BuilderResponse, FixerResponse, Critique])
def test_response_schemas_export_json_schema(schema: type[BaseModel]) -> None:
    assert schema.model_json_schema()["type"] == "object"


def test_telemetry_record_keeps_javascript_field_names(hld_state: dict[str, Any]) -> None:
    record = TelemetryRecord(video_t=1.5, state=GameState.model_validate(hld_state))

    line = record.model_dump_json()

    assert '"levelComplete":false' in line
    assert TelemetryRecord.model_validate_json(line) == record


def test_artifacts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SmokeReport.model_validate({"passed": True, "checks": [], "attempt": 1, "extra": 1})
