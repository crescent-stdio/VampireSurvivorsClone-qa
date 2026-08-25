"""Post-run LLM inspection of sanitized gameplay observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .memory import sanitize_agent_channel
from .planners import LLMPlanner, compact_observation
from .state_channels import build_agent_observation


INSPECTION_SCHEMA_V1 = "qa-inspection/v1"
INSPECTION_SCHEMA_V2 = "qa-inspection/v2"
INSPECTION_NORMALIZER_VERSION = "qa-inspection-normalizer/v2"
INSPECTION_CHUNK_SIZE = 32
INSPECTION_CHUNK_OVERLAP = 2
MAX_MERGED_FINDING_STATEMENT_CHARS = 2000
_MERGED_STATEMENT_SEPARATOR = " | "


INSPECTOR_SYSTEM_PROMPT = """You are a QA engineer auditing a recorded gameplay trace for internal inconsistencies.
You are given one chunk of sanitized observations in order. An observation may include action_context describing only the upgrade selection or restart action the player actually performed immediately before that observation. Judge only relationships you can compute from the fields present. Do not infer unavailable state, source code, scenario intent, evaluator output, or hidden causes.
For a numeric finding return kind=numeric, a real field path, an explicit comparison operator, expected_value, observed_value, a short statement, and evidence_refs from this chunk. For a behavior finding return kind=behavior, a generic rule, expected_value, observed_value, a short statement, and evidence_refs from this chunk.
Return a finding only for an observed inconsistency. An empty findings list is correct for a clean chunk."""


class StrictInspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


EvidenceRef = Annotated[str, Field(min_length=1)]


class NumericFinding(StrictInspectionModel):
    kind: Literal["numeric"]
    field: str = Field(min_length=1)
    comparison: Literal["==", "!=", "<", "<=", ">", ">="]
    expected_value: float = Field(allow_inf_nan=False)
    observed_value: float = Field(allow_inf_nan=False)
    statement: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class BehaviorFinding(StrictInspectionModel):
    kind: Literal["behavior"]
    rule: str = Field(min_length=1)
    expected_value: str = Field(min_length=1)
    observed_value: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


InspectionFinding = Annotated[
    NumericFinding | BehaviorFinding,
    Field(discriminator="kind"),
]
_FINDING_ADAPTER = TypeAdapter(InspectionFinding)


class InspectionArtifactV2(StrictInspectionModel):
    schema_version: Literal["qa-inspection/v2"]
    findings: list[InspectionFinding]


def _structured_outputs_schema(node: Any) -> Any:
    """Rewrite a Pydantic schema into the subset Structured Outputs accepts.

    Pydantic serialises the discriminated finding union as `oneOf` plus a
    `discriminator`. The endpoint supports `anyOf` and rejects both of those with
    HTTP 400, which the planner surfaces only as a transport error, so a contract
    break reads like an outage. The variants stay ordered and `kind` stays a
    Literal on each model, so the union remains unambiguous without them.
    """

    if isinstance(node, dict):
        rewritten = {
            key: _structured_outputs_schema(value)
            for key, value in node.items()
            if key != "discriminator"
        }
        if "oneOf" in rewritten:
            rewritten["anyOf"] = rewritten.pop("oneOf")
        return rewritten
    if isinstance(node, list):
        return [_structured_outputs_schema(item) for item in node]
    return node


FINDINGS_SCHEMA = _structured_outputs_schema(InspectionArtifactV2.model_json_schema())


def validate_inspection_artifact_v2(response: Any) -> dict[str, Any]:
    """Validate the strict current inspection contract without legacy coercion."""

    return InspectionArtifactV2.model_validate(response).model_dump()


def _public_action_context(transition: dict[str, Any]) -> dict[str, Any] | None:
    decision = transition.get("decision")
    if not isinstance(decision, dict) or decision.get("tool", "game") != "game":
        return None
    action = decision.get("action")
    if action == "restart":
        return {"action": "restart"}
    if action != "select_upgrade":
        return None
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        return None
    index = arguments.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    return {"action": "select_upgrade", "selection_index": index}


def build_inspection_payload(transitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a recorded trace to the sanitized observation channel for inspection."""
    observations: list[dict[str, Any]] = []
    for transition in transitions:
        raw = transition.get("observation")
        if not isinstance(raw, dict):
            continue
        compact = compact_observation(build_agent_observation(raw))
        compact["observation_id"] = str(raw.get("observation_id") or "")
        action_context = _public_action_context(transition)
        if action_context is not None:
            compact["action_context"] = action_context
        observations.append(compact)
    return sanitize_agent_channel(
        {"schema_version": INSPECTION_SCHEMA_V2, "observations": observations}
    )


def build_inspection_chunks(transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split every sanitized trace into stable, generic overlapping observation chunks."""
    observations = build_inspection_payload(transitions).get("observations") or []
    chunks: list[dict[str, Any]] = []
    stride = INSPECTION_CHUNK_SIZE - INSPECTION_CHUNK_OVERLAP
    for start in range(0, len(observations), stride):
        chunk_observations = observations[start : start + INSPECTION_CHUNK_SIZE]
        if not chunk_observations:
            continue
        end = start + len(chunk_observations) - 1
        chunks.append(
            {
                "schema_version": INSPECTION_SCHEMA_V2,
                "chunk_id": f"inspection-chunk-{start:06d}-{end:06d}",
                "observations": chunk_observations,
            }
        )
        if end == len(observations) - 1:
            break
    return chunks


def _normalize_v1_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "numeric",
        "field": item.get("field"),
        "comparison": "!=",
        "expected_value": item.get("computed_value"),
        "observed_value": item.get("reported_value"),
        "statement": item.get("statement"),
        "evidence_refs": item.get("evidence_refs"),
    }


def _deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated chunk findings by their public relation, never oracle data."""
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in findings:
        if finding["kind"] == "numeric":
            key = (
                "numeric",
                finding["field"],
                finding["comparison"],
                finding["expected_value"],
                finding["observed_value"],
            )
        else:
            key = (
                "behavior",
                finding["rule"],
                finding["expected_value"],
                finding["observed_value"],
            )
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = finding
            continue
        existing["evidence_refs"] = list(
            dict.fromkeys([*existing["evidence_refs"], *finding["evidence_refs"]])
        )
        statements = existing["statement"].split(_MERGED_STATEMENT_SEPARATOR)
        statement = finding["statement"]
        if statement not in statements:
            merged = _MERGED_STATEMENT_SEPARATOR.join([*statements, statement])
            if len(merged) <= MAX_MERGED_FINDING_STATEMENT_CHARS:
                existing["statement"] = merged
    return list(deduplicated.values())


def normalize_inspection_artifact(response: Any) -> dict[str, Any]:
    """Strictly normalize v2 findings and convert readable v1 artifacts to v2."""
    if not isinstance(response, dict):
        return {"schema_version": INSPECTION_SCHEMA_V2, "findings": []}
    source_version = response.get("schema_version")
    is_v1 = source_version in (None, INSPECTION_SCHEMA_V1)
    if not is_v1 and source_version != INSPECTION_SCHEMA_V2:
        return {"schema_version": INSPECTION_SCHEMA_V2, "findings": []}
    raw_findings = response.get("findings")
    if not isinstance(raw_findings, list):
        return {"schema_version": INSPECTION_SCHEMA_V2, "findings": []}
    findings: list[dict[str, Any]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        candidate = _normalize_v1_finding(item) if is_v1 else item
        try:
            findings.append(_FINDING_ADAPTER.validate_python(candidate).model_dump())
        except ValidationError:
            continue
    return {
        "schema_version": INSPECTION_SCHEMA_V2,
        "findings": _deduplicate_findings(findings),
    }


def normalize_findings(response: Any) -> list[dict[str, Any]]:
    """Compatibility reader returning normalized v2 findings for existing callers."""
    return normalize_inspection_artifact(response)["findings"]


def inspect_trace(
    planner: LLMPlanner,
    transitions: list[dict[str, Any]],
    on_request_complete: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Inspect every generic trace chunk and return one deduplicated v2 artifact."""
    chunks = build_inspection_chunks(transitions)
    if not chunks:
        return {"schema_version": INSPECTION_SCHEMA_V2, "findings": []}
    findings: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            response = planner._request(
                INSPECTOR_SYSTEM_PROMPT,
                json.dumps(chunk, ensure_ascii=False),
                response_schema=FINDINGS_SCHEMA,
                include_planning_history=False,
            )
        finally:
            if on_request_complete is not None:
                on_request_complete()
        findings.extend(normalize_findings(response))
    return normalize_inspection_artifact(
        {"schema_version": INSPECTION_SCHEMA_V2, "findings": findings}
    )
