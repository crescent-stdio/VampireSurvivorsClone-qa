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


def _claim_text(decision: dict[str, Any]) -> str:
    reflection = decision.get("reflection")
    parts = [str(decision.get(field) or "") for field in AGENT_TEXT_FIELDS]
    if isinstance(reflection, dict):
        parts.append(str(reflection.get("summary") or ""))
    return " ".join(part for part in parts if part)


def agent_claims(
    transitions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]] | None,
    llm_assessment: dict[str, Any] | None,
) -> list[AgentClaim]:
    """Every surface on which the agent could have written a finding."""
    claims: list[AgentClaim] = []
    for transition in transitions:
        decision = transition.get("decision")
        if not isinstance(decision, dict):
            continue
        text = _claim_text(decision)
        if not text.strip():
            continue
        reflection = decision.get("reflection")
        refs = list((reflection or {}).get("evidence_refs") or []) if isinstance(reflection, dict) else []
        claims.append(
            AgentClaim(surface="reflection", text=text, evidence_refs=[str(ref) for ref in refs])
        )
    for state in hypotheses or []:
        statement = str(state.get("statement") or "")
        if statement.strip():
            claims.append(
                AgentClaim(
                    surface="hypothesis",
                    text=statement,
                    evidence_refs=[str(ref) for ref in state.get("evidence_refs") or []],
                )
            )
    if llm_assessment:
        # No response_schema is enforced on the assessment, so the shape is unknown;
        # dump the whole thing and scrape any transition ids out of it.
        blob = json.dumps(llm_assessment, ensure_ascii=False)
        claims.append(
            AgentClaim(
                surface="assessment",
                text=blob,
                evidence_refs=re.findall(r"[0-9a-f]{32}-(?:obs|event)-[0-9a-f]+", blob),
            )
        )
    return claims


def scored_inspection_findings(
    inspection: dict[str, Any] | None, fault_refs: list[str], tolerance: float = 1e-6
) -> list[dict[str, Any]]:
    """Findings whose own numbers disagree, on a transition the oracle rejects.

    Scored numerically rather than by keyword. The rubric undercounted real finds three
    separate times on wording alone -- "exceed" against "exceeds", "inconsistency"
    against "inconsistent", "disagree" against "does not match" -- and every fix was
    another synonym nudging the score upward. A model cannot earn a match here by
    phrasing, only by producing two values that differ.
    """
    flagged = set(fault_refs)
    scored: list[dict[str, Any]] = []
    for finding in (inspection or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        try:
            computed = float(finding["computed_value"])
            reported = float(finding["reported_value"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(computed - reported) <= tolerance:
            continue  # the agent checked and the numbers agreed; that is not a finding
        cited = [
            str(ref) for ref in finding.get("evidence_refs") or [] if str(ref) in flagged
        ]
        if cited:
            scored.append({**finding, "cited_evidence_refs": cited})
    return scored


def _terms_hit(text: str, terms: list[str]) -> list[str]:
    """Match on word boundaries, not raw substrings.

    A substring scan makes short topics catastrophically broad: "exp" and "xp" are
    both inside "expected", so "the expected effect does not match the observation"
    -- a sentence about steering -- scored a match for the experience-drift fault.
    """
    normalized = normalize(text)
    return [
        term
        for term in terms
        if re.search(rf"\b{re.escape(normalize(term))}\b", normalized)
    ]


def has_strong_claim(
    hypotheses: list[dict[str, Any]] | None,
    llm_assessment: dict[str, Any] | None,
    inspection: dict[str, Any] | None = None,
) -> bool:
    """A confirmed hypothesis or a reported bug candidate.

    A bare `reflection.status == "unexpected"` is exploration and deliberately does not
    count: the planner prompt makes raising one cheap on purpose, and penalising that on
    control runs would train the agent back into silence.
    """
    if any(str(state.get("status") or "") == "confirmed" for state in hypotheses or []):
        return True
    if scored_inspection_findings(inspection, []) or (inspection or {}).get("findings"):
        return True
    candidates = (llm_assessment or {}).get("bug_candidates")
    return bool(isinstance(candidates, list) and candidates)


def score_agent_detection(
    *,
    fault_id: str | None,
    policy: str,
    trace_completeness: str,
    has_agent_text_channel: bool | None = None,
    inspection: dict[str, Any] | None = None,
    oracle_verdict: str,
    transitions: list[dict[str, Any]],
    fault_refs: list[str],
    hypotheses: list[dict[str, Any]] | None = None,
    llm_assessment: dict[str, Any] | None = None,
    rubric: DetectionRubric | None = None,
) -> DetectionResult:
    """Decide whether the agent reported the injected fault. Never raises."""
    try:
        return _score(
            fault_id=fault_id,
            policy=policy,
            trace_completeness=trace_completeness,
            has_agent_text_channel=has_agent_text_channel,
            inspection=inspection,
            oracle_verdict=oracle_verdict,
            transitions=transitions,
            fault_refs=fault_refs,
            hypotheses=hypotheses,
            llm_assessment=llm_assessment,
            rubric=rubric,
        )
    except Exception as error:  # never break the verdict path over a scoring bug
        return DetectionResult(status="not_evaluated", reason=f"scoring failed: {error}")


def _score(
    *,
    fault_id: str | None,
    policy: str,
    trace_completeness: str,
    has_agent_text_channel: bool | None,
    inspection: dict[str, Any] | None,
    oracle_verdict: str,
    transitions: list[dict[str, Any]],
    fault_refs: list[str],
    hypotheses: list[dict[str, Any]] | None,
    llm_assessment: dict[str, Any] | None,
    rubric: DetectionRubric | None,
) -> DetectionResult:
    rubric = rubric or load_rubric()
    version = rubric.rubric_version

    # `policy` says who drove the game; it used to double as "is there agent prose to
    # score". Those diverge once an inspector can run on a heuristic-driven session, so
    # the caller states it explicitly. Defaulting from policy keeps every existing
    # heuristic artifact scoring not_evaluated rather than becoming eligible for
    # false_positive.
    if has_agent_text_channel is None:
        has_agent_text_channel = policy in ("llm", "hybrid")
    if not has_agent_text_channel:
        return DetectionResult(
            status="not_evaluated",
            reason=f"policy {policy!r} produced no agent text to score",
            rubric_version=version,
        )

    strong = has_strong_claim(hypotheses, llm_assessment, inspection)
    if not fault_id:
        if strong:
            return DetectionResult(
                status="false_positive",
                reason="the agent reported a bug on a run with no injected fault",
                rubric_version=version,
            )
        return DetectionResult(
            status="not_evaluated",
            reason="control run; silence is correct behavior rather than a measurable result",
            rubric_version=version,
        )

    entry = rubric.for_fault(fault_id)
    if entry is None:
        return DetectionResult(
            status="not_evaluated",
            reason=f"no rubric entry for {fault_id!r}",
            rubric_version=version,
        )
    if not entry.observable_in_agent_channel:
        return DetectionResult(
            status="not_evaluated",
            reason=entry.unobservable_reason,
            rubric_version=version,
        )
    if oracle_verdict != "fail" or not fault_refs:
        return DetectionResult(
            status="not_evaluated",
            reason="the fault never became observable in this trace",
            rubric_version=version,
        )

    # Structured findings first: they carry their own evidence and need no vocabulary.
    inspected = scored_inspection_findings(inspection, fault_refs)
    if inspected:
        first = inspected[0]
        return DetectionResult(
            status="match",
            reason=(
                f"the inspector computed {first['computed_value']} for {first['field']} "
                f"where the observation reported {first['reported_value']}"
            ),
            matched_terms=[str(first.get("field") or "")],
            matched_surfaces=["inspection"],
            cited_evidence_refs=list(first["cited_evidence_refs"]),
            rubric_version=version,
        )

    flagged = set(fault_refs)
    for claim in agent_claims(transitions, hypotheses, llm_assessment):
        topics = _terms_hit(claim.text, entry.topic_terms)
        symptoms = _terms_hit(claim.text, entry.symptom_terms)
        if not topics or not symptoms:
            continue
        cited = [ref for ref in claim.evidence_refs if ref in flagged]
        if cited:
            return DetectionResult(
                status="match",
                reason="the agent named the defect while citing a transition the oracle rejects",
                matched_terms=sorted({*topics, *symptoms}),
                matched_surfaces=[claim.surface],
                cited_evidence_refs=cited,
                rubric_version=version,
            )

    if trace_completeness == "partial":
        return DetectionResult(
            status="not_evaluated",
            reason="a partial trace cannot establish that the agent never reported it",
            rubric_version=version,
        )
    return DetectionResult(
        status="miss",
        reason="the agent never described the defect in its own text",
        rubric_version=version,
    )
