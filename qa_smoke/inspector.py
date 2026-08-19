"""Post-run LLM inspection of a recorded trace.

Steering and bug detection are different jobs. Steering needs a cheap, fast model
called once per step with the whole conversation; detection needs arithmetic and
careful reading, once, over the observations alone. Splitting them lets a stronger
model do the part it is good at for a fraction of a steering run's cost.

The inspector never sees evaluator state. Findings come back structured, so they are
scored by comparing numbers rather than by matching adjectives.
"""

from __future__ import annotations

import json
from typing import Any

from .memory import sanitize_agent_channel
from .planners import LLMPlanner, compact_observation
from .state_channels import build_agent_observation


INSPECTOR_SYSTEM_PROMPT = """You are a QA engineer auditing a recorded gameplay trace for internal inconsistencies.
You are given observations in order. Judge each observation on its own: the numbers inside one observation must agree with each other, regardless of whether the game behaved plausibly over time.
Check relationships you can compute from the fields present: a field named as a ratio or fraction against the pair of fields it summarizes and against the range 0 to 1, a part against its whole, a count against the list it counts, an offset between two entities against the positions it is derived from.
Report a finding only when a value you computed disagrees with a value the observation reports, or a quantity falls outside the range its name implies. Do not report advisory scores, tactical difficulty, or a game simply going badly for the player.
For every finding give the field path, the value you computed, the value the observation reports, a one-sentence statement, and the observation_id it came from.
Return findings only for observations you actually checked. An empty findings list is the correct answer for a clean trace."""

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "computed_value": {"type": "number"},
                    "reported_value": {"type": "number"},
                    "statement": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "field",
                    "computed_value",
                    "reported_value",
                    "statement",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def build_inspection_payload(transitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a recorded trace to what the inspector may see.

    recorder.steps carries the RAW bridge observation -- player_view and all. Three
    layers are needed and none is redundant: build_agent_observation strips the
    evaluator and view channels, compact_observation whitelists and caps the rest, and
    sanitize_agent_channel is the only one that removes oracle/verdict/manifest keys.
    The envelope is sanitized too, not just each observation.
    """
    observations: list[dict[str, Any]] = []
    for transition in transitions:
        raw = transition.get("observation")
        if not isinstance(raw, dict):
            continue
        compact = compact_observation(build_agent_observation(raw))
        compact["observation_id"] = str(raw.get("observation_id") or "")
        observations.append(compact)
    return sanitize_agent_channel({"observations": observations})


def normalize_findings(response: Any) -> list[dict[str, Any]]:
    """Coerce whatever came back into a list of well-formed findings."""
    if not isinstance(response, dict):
        return []
    findings: list[dict[str, Any]] = []
    for item in response.get("findings") or []:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(
                {
                    "field": str(item.get("field") or ""),
                    "computed_value": float(item["computed_value"]),
                    "reported_value": float(item["reported_value"]),
                    "statement": str(item.get("statement") or ""),
                    "evidence_refs": [
                        str(ref) for ref in item.get("evidence_refs") or [] if str(ref)
                    ],
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return findings


def inspect_trace(
    planner: LLMPlanner, transitions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run one inspection call and return `{findings: [...]}`."""
    payload = build_inspection_payload(transitions)
    if not payload.get("observations"):
        return {"findings": []}
    response = planner._request(
        INSPECTOR_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False),
        response_schema=FINDINGS_SCHEMA,
        include_planning_history=False,
    )
    return {"findings": normalize_findings(response)}
