"""Score whether the QA agent itself reported the injected fault.

This is deliberately a keyword rubric over the agent's own words, not a judgement
about whether a bug is real. `reporting.Annotation` and `aggregate_annotations`
-- the three-reviewer majority protocol -- remain the sole authority on bug
validity, and nothing here writes to `annotations.jsonl`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parents[1] / "config" / "qa-detection-rubric.json"
RUBRIC_SCHEMA = "qa-detection-rubric/v1"

AUTHORITY_NOTE = (
    "Automatic keyword rubric over the agent's own text. This is NOT a human "
    "annotation: a match means the agent wrote words identifying the defect while "
    "citing a transition the oracle objects to, not that a reviewer confirmed a bug. "
    "annotations.jsonl and reporting.aggregate_annotations remain the sole authority "
    "on bug validity, and this score is never merged into them."
)

# Free-text fields the agent authors. decision.arguments is deliberately excluded:
# the charter injects interrupt_health_ratio into every direct_steer, which makes a
# naive whole-decision scan match "health ratio" on 16 of 18 steps of a run where the
# agent never mentioned it once.
AGENT_TEXT_FIELDS = ("plan", "hypothesis", "qa_observation", "expected_effect")


class DetectionContractError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FaultRubric(StrictModel):
    bug_id: str | None = None
    observable_in_agent_channel: bool
    unobservable_reason: str = ""
    topic_terms: list[str]
    symptom_terms: list[str]


class DetectionRubric(StrictModel):
    schema_id: str
    rubric_version: str
    faults: dict[str, FaultRubric]

    def for_fault(self, fault_id: str) -> FaultRubric | None:
        return self.faults.get(fault_id)


class AgentClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface: Literal["reflection", "hypothesis", "assessment"]
    text: str
    evidence_refs: list[str]


class DetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["match", "miss", "false_positive", "not_evaluated"]
    reason: str
    matched_terms: list[str] = []
    matched_surfaces: list[str] = []
    cited_evidence_refs: list[str] = []
    rubric_version: str = ""


def normalize(text: str) -> str:
    """Fold the agent's prose and the rubric's phrases onto one comparable form."""
    lowered = str(text).lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", lowered).strip()


def load_rubric(path: Path | None = None) -> DetectionRubric:
    source = path or DEFAULT_RUBRIC_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DetectionContractError(f"unreadable detection rubric {source}: {error}") from error
    if payload.get("schema") != RUBRIC_SCHEMA:
        raise DetectionContractError(
            f"detection rubric schema must be {RUBRIC_SCHEMA}, got {payload.get('schema')!r}"
        )
    faults = payload.get("faults")
    if not isinstance(faults, dict) or not faults:
        raise DetectionContractError("detection rubric must define at least one fault")
    rubric = DetectionRubric(
        schema_id=RUBRIC_SCHEMA,
        rubric_version=str(payload.get("rubric_version") or ""),
        faults={key: FaultRubric.model_validate(value) for key, value in faults.items()},
    )
    for fault_id, entry in rubric.faults.items():
        if not entry.observable_in_agent_channel and not entry.unobservable_reason:
            raise DetectionContractError(
                f"fault {fault_id} is marked unobservable without a reason"
            )
        if not entry.topic_terms or not entry.symptom_terms:
            raise DetectionContractError(f"fault {fault_id} needs topic and symptom terms")
    return rubric
