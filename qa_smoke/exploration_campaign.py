"""Track A clean-build autonomous exploration and evidence-linked candidate reports."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from .adapters import read_final_screenshot_metadata
from .detection_campaign import (
    CAMPAIGN_MANIFEST_SCHEMA,
    INSPECTION_REPETITIONS,
    INSPECTION_REQUEST_CONTRACT_VERSION,
    INSPECTOR_EFFORT,
    INSPECTOR_MODEL,
    LOGICAL_INSPECTION_CALL_CAP,
    SEEDS,
    STEERING_MODEL,
    CampaignContractError,
    CampaignExecutionError,
    CheckpointStore,
    InspectionCallBudget,
    InspectionPassFailure,
    InspectorAdapter,
    _atomic_write_json,
    _file_hash,
    _invalidate_trace_inspections,
    _inspection_request_contract_digest,
    _max_chunks_for_steps,
    _opaque_trace_id,
    _prepare_campaign_output,
    _public_api_endpoint_hash,
    _public_error_type,
    _read_json_object,
    _sha256,
    hash_path,
    inspect_trace_pass,
)
from .inspector import (
    INSPECTOR_SYSTEM_PROMPT,
    INSPECTION_NORMALIZER_VERSION,
    MAX_MERGED_FINDING_STATEMENT_CHARS,
    normalize_inspection_artifact,
)
from .memory import sanitize_error_type as _sanitize_error_type
from .planners import (
    PLANNING_MAX_TOKENS,
    LLMPlanner,
    build_decision_response_schema,
    resolve_llm_api_url,
)
from .reporting import RunRecorder
from .run import parse_args as parse_run_args
from .run import run_session
from .screenshot_evidence import finalize_evidence_screenshot


EXPLORATION_REPORT_SCHEMA = "qa-exploration-report/v1"
TRACK_A_PRESET = "smoke"
PLANNED_INSPECTION_CALLS = 189
POC_TRACK_A_MISSION_IDS = (
    "core-combat-survival",
    "core-progression-upgrades",
    "core-death-restart-isolation",
)
POC_TRACK_A_SEEDS = (9101, 9102)
STEERING_PROMPT_TEMPLATE_VERSION = str(
    RunRecorder.__dataclass_fields__["prompt_version"].default
)
STEERING_PLAN_HORIZON_SECONDS = 5.0
MAX_MARKDOWN_EVIDENCE_ROWS_PER_CANDIDATE = 12


@dataclass(frozen=True)
class ExplorationMission:
    mission_id: str
    tier: Literal["core", "long"]
    objective: str
    max_simulation_seconds: float
    max_steps: int


MISSIONS = (
    ExplorationMission(
        "core-menu-level1",
        "core",
        "Start from the menu, enter Level 1, and observe the initial playable state.",
        60.0,
        20,
    ),
    ExplorationMission(
        "core-combat-survival",
        "core",
        "Exercise ordinary combat and survival while observing state consistency.",
        120.0,
        40,
    ),
    ExplorationMission(
        "core-progression-upgrades",
        "core",
        "Progress through levels and exercise upgrade selections and their visible effects.",
        240.0,
        80,
    ),
    ExplorationMission(
        "core-items-chests-inventory",
        "core",
        "Explore items, chests, inventory changes, and related visible transitions.",
        240.0,
        80,
    ),
    ExplorationMission(
        "core-death-restart-isolation",
        "core",
        "Exercise death and restart behavior and observe whether run state remains isolated.",
        240.0,
        80,
    ),
    ExplorationMission(
        "long-sustained-combat",
        "long",
        "Sustain ordinary combat for a long trace while preserving survival when possible.",
        240.0,
        80,
    ),
    ExplorationMission(
        "long-multi-level-growth-upgrades",
        "long",
        "Exercise multiple levels, growth, and varied upgrade combinations over a long trace.",
        240.0,
        80,
    ),
    ExplorationMission(
        "long-items-chests-area-effects",
        "long",
        "Exercise item, chest, and area-effect combinations over a long trace.",
        240.0,
        80,
    ),
)


@dataclass(frozen=True)
class ExplorationEpisodeSpec:
    unit_id: str
    mission_id: str
    tier: Literal["core", "long"]
    seed: int
    goal: str
    max_simulation_seconds: float
    max_steps: int
    variant: str = "clean"
    preset: str = TRACK_A_PRESET
    faults: tuple[str, ...] = ()
    driver: str = "llm"
    model: str = STEERING_MODEL
    source_tools_enabled: bool = False
    nested_inspector_enabled: bool = False


@dataclass(frozen=True)
class ExplorationEpisodeResult:
    transitions: Sequence[dict[str, Any]]
    execution_status: str = "completed"
    coverage_status: str = "reached"
    launch_faults: tuple[str, ...] = ()
    invariant_validations: Sequence[dict[str, Any]] = field(default_factory=tuple)
    harness_failures: Sequence[dict[str, Any]] = field(default_factory=tuple)
    trace_evidence_refs: Sequence[str] = field(default_factory=tuple)
    error: str = ""
    screenshot_path: str | None = None
    screenshot_error: str = ""
    screenshot_retention_axes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "transitions": list(self.transitions),
            "execution_status": self.execution_status,
            "coverage_status": self.coverage_status,
            "launch_faults": list(self.launch_faults),
            "invariant_validations": list(self.invariant_validations),
            "harness_failures": list(self.harness_failures),
            "trace_evidence_refs": list(self.trace_evidence_refs),
            "error": _public_error_type(self.error),
            "screenshot_path": self.screenshot_path,
            "screenshot_error": _public_error_type(self.screenshot_error),
            "screenshot_retention_axes": list(self.screenshot_retention_axes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExplorationEpisodeResult":
        transitions = value.get("transitions")
        if not isinstance(transitions, list) or any(
            not isinstance(item, dict) for item in transitions
        ):
            raise CampaignContractError("exploration transitions must be a list of objects")
        launch_faults = value.get("launch_faults") or []
        validations = value.get("invariant_validations") or []
        failures = value.get("harness_failures") or []
        trace_evidence_refs = value.get("trace_evidence_refs") or []
        screenshot_retention_axes = value.get("screenshot_retention_axes") or []
        if not isinstance(launch_faults, list) or any(
            not isinstance(item, str) for item in launch_faults
        ):
            raise CampaignContractError("exploration launch faults must be a list of strings")
        if not isinstance(validations, list) or any(
            not isinstance(item, dict) for item in validations
        ):
            raise CampaignContractError("invariant validations must be a list of objects")
        if not isinstance(failures, list) or any(
            not isinstance(item, dict) for item in failures
        ):
            raise CampaignContractError("harness failures must be a list of objects")
        if not isinstance(trace_evidence_refs, list) or any(
            not isinstance(item, str) for item in trace_evidence_refs
        ):
            raise CampaignContractError("trace evidence refs must be a list of strings")
        if not isinstance(screenshot_retention_axes, list) or any(
            not isinstance(item, str) for item in screenshot_retention_axes
        ):
            raise CampaignContractError(
                "screenshot retention axes must be a list of strings"
            )
        return cls(
            transitions=transitions,
            execution_status=str(value.get("execution_status") or "schema_error"),
            coverage_status=str(value.get("coverage_status") or "error"),
            launch_faults=tuple(launch_faults),
            invariant_validations=tuple(validations),
            harness_failures=tuple(failures),
            trace_evidence_refs=tuple(trace_evidence_refs),
            error=str(value.get("error") or ""),
            screenshot_path=(
                str(value["screenshot_path"]) if value.get("screenshot_path") else None
            ),
            screenshot_error=str(value.get("screenshot_error") or ""),
            screenshot_retention_axes=tuple(screenshot_retention_axes),
        )


class ExplorationBackend(Protocol):
    def run_autonomous(
        self,
        spec: ExplorationEpisodeSpec,
        output_dir: Path,
    ) -> ExplorationEpisodeResult: ...


@dataclass(frozen=True)
class ExplorationTraceRecord:
    spec: ExplorationEpisodeSpec
    opaque_trace_id: str
    result: ExplorationEpisodeResult
    inspection_passes: Sequence[dict[str, Any] | None]
    resumed: bool = False


@dataclass(frozen=True)
class ExplorationCampaignConfig:
    build: Path
    project_root: Path
    output: Path
    headless: bool = False
    quiet: bool = False
    api_url: str | None = None
    resume: bool = False
    profile: Literal["full", "poc"] = "full"


@dataclass(frozen=True)
class ExplorationCampaignResult:
    manifest_path: Path
    report_path: Path
    records: tuple[ExplorationTraceRecord, ...]
    resumed: bool


def build_track_a_schedule(
    profile: Literal["full", "poc"] = "full",
) -> tuple[ExplorationEpisodeSpec, ...]:
    """Build the full acceptance schedule or the fixed PoC subset."""

    if profile not in {"full", "poc"}:
        raise CampaignContractError("Track A profile must be full or poc")
    missions = (
        MISSIONS
        if profile == "full"
        else tuple(
            mission for mission in MISSIONS if mission.mission_id in POC_TRACK_A_MISSION_IDS
        )
    )
    seeds = SEEDS if profile == "full" else POC_TRACK_A_SEEDS

    schedule = tuple(
        ExplorationEpisodeSpec(
            unit_id=f"exploration/{mission.mission_id}/{seed}",
            mission_id=mission.mission_id,
            tier=mission.tier,
            seed=seed,
            goal=mission.objective,
            max_simulation_seconds=mission.max_simulation_seconds,
            max_steps=mission.max_steps,
        )
        for mission in missions
        for seed in seeds
    )
    planned = sum(
        _max_chunks_for_steps(spec.max_steps) * INSPECTION_REPETITIONS
        for spec in schedule
    )
    expected_count = 24 if profile == "full" else 6
    if len(schedule) != expected_count or (
        profile == "full" and planned != PLANNED_INSPECTION_CALLS
    ):
        raise CampaignContractError("Track A schedule no longer matches fixed acceptance totals")
    for spec in schedule:
        validate_track_a_spec(spec)
    return schedule


def _planned_track_a_inspection_calls(
    schedule: Sequence[ExplorationEpisodeSpec],
) -> int:
    return sum(
        _max_chunks_for_steps(spec.max_steps) * INSPECTION_REPETITIONS
        for spec in schedule
    )


def validate_track_a_spec(spec: ExplorationEpisodeSpec) -> None:
    """Reject any Track A launch that is not clean, pure LLM, and tool-blind."""

    if spec.variant != "clean" or spec.faults:
        raise CampaignContractError("Track A requires a clean launch with an empty fault list")
    if spec.driver != "llm" or spec.model != STEERING_MODEL:
        raise CampaignContractError("Track A requires the fixed pure LLM steering model")
    if spec.source_tools_enabled:
        raise CampaignContractError("Track A source tools must remain disabled")
    if spec.nested_inspector_enabled:
        raise CampaignContractError("Track A nested run-session inspection must remain disabled")
    if spec.seed not in SEEDS or spec.max_steps <= 0 or spec.max_simulation_seconds <= 0:
        raise CampaignContractError("Track A mission limits or seed are invalid")


def _normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9._]+", "-", str(value or "").strip().lower()).strip("-")


def _numeric_rule(comparison: Any) -> str:
    names = {
        "==": "numeric-equal",
        "!=": "numeric-not-equal",
        "<": "numeric-less-than",
        "<=": "numeric-less-or-equal",
        ">": "numeric-greater-than",
        ">=": "numeric-greater-or-equal",
    }
    return names.get(str(comparison), "numeric-relation")


_PLANNER_NUMERIC_OPERATORS = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}
_PLANNER_FIELD_SEGMENT = r"[a-zA-Z_][a-zA-Z0-9_]*(?:\[[0-9]+\])*"
_PLANNER_FIELD_PATH_PATTERN = re.compile(
    rf"{_PLANNER_FIELD_SEGMENT}(?:\.{_PLANNER_FIELD_SEGMENT})*"
)
_PLANNER_NUMERIC_CANDIDATE_PATTERN = re.compile(
    rf"numeric:(eq|ne|lt|le|gt|ge):({_PLANNER_FIELD_SEGMENT}"
    rf"(?:\.{_PLANNER_FIELD_SEGMENT})*)"
)


def _normalize_field_token(field_name: Any) -> str:
    candidate = str(field_name or "").strip().lower()
    if _PLANNER_FIELD_PATH_PATTERN.fullmatch(candidate):
        return candidate
    return _normalize_token(candidate)


def _planner_numeric_relation(candidate_id: str) -> tuple[str, str] | None:
    """Parse only the explicit planner numeric contract, never free-form prose."""

    match = _PLANNER_NUMERIC_CANDIDATE_PATTERN.fullmatch(candidate_id.strip())
    if match is None:
        return None
    return _PLANNER_NUMERIC_OPERATORS[match.group(1)], match.group(2)


def _observation_context(
    transitions: Sequence[dict[str, Any]],
    trace_evidence_refs: Sequence[str] = (),
) -> dict[str, tuple[str, str]]:
    contexts: dict[str, tuple[str, str]] = {}
    for transition in transitions:
        raw = transition.get("observation")
        if not isinstance(raw, dict):
            continue
        phase = _normalize_token(raw.get("phase") or "unknown") or "unknown"
        event_state = raw.get("event_state")
        event = ""
        if isinstance(event_state, dict):
            event = _normalize_token(event_state.get("type") or event_state.get("event") or "")
            event_id = str(event_state.get("event_id") or "").strip()
            if event_id:
                contexts[event_id] = (phase, event)
        observation_id = str(raw.get("observation_id") or "").strip()
        if observation_id:
            contexts[observation_id] = (phase, event)
    for evidence_ref in trace_evidence_refs:
        if evidence_ref:
            contexts[evidence_ref] = ("termination", "termination")
    return contexts


def _evidence_context(
    refs: Any,
    contexts: Mapping[str, tuple[str, str]],
) -> tuple[tuple[str, ...], str, str] | None:
    if not isinstance(refs, list):
        return None
    normalized = tuple(dict.fromkeys(str(item).strip() for item in refs if str(item).strip()))
    if not normalized or any(reference not in contexts for reference in normalized):
        return None
    phase, event = contexts[normalized[0]]
    return normalized, phase, event


CandidateKey = tuple[str, str, str, str, str]


def _candidate_key(
    category: Any,
    rule: Any,
    field_name: Any,
    phase: Any,
    event: Any,
) -> CandidateKey:
    return (
        _normalize_token(category) or "behavior",
        _normalize_token(rule) or "unspecified-rule",
        _normalize_field_token(field_name),
        _normalize_token(phase) or "unknown",
        _normalize_token(event),
    )


def _planner_occurrences(record: ExplorationTraceRecord) -> list[dict[str, Any]]:
    contexts = _observation_context(
        record.result.transitions,
        record.result.trace_evidence_refs,
    )
    occurrences: list[dict[str, Any]] = []
    for transition in record.result.transitions:
        decision = transition.get("decision")
        if not isinstance(decision, dict):
            continue
        reflection = decision.get("reflection")
        if not isinstance(reflection, dict) or reflection.get("status") != "unexpected":
            continue
        statement = str(
            decision.get("qa_observation") or reflection.get("summary") or ""
        ).strip()
        rule = str(reflection.get("candidate_id") or "").strip()
        evidence = _evidence_context(reflection.get("evidence_refs"), contexts)
        if not statement or not rule or evidence is None:
            continue
        refs, phase, event = evidence
        numeric_relation = _planner_numeric_relation(rule)
        if numeric_relation is None:
            key = _candidate_key("behavior", rule, "", phase, event)
        else:
            comparison, field_name = numeric_relation
            key = _candidate_key(
                "numeric",
                _numeric_rule(comparison),
                field_name,
                phase,
                event,
            )
        occurrences.append(
            {
                "key": key,
                "source": "planner",
                "trace_id": record.opaque_trace_id,
                "mission_id": record.spec.mission_id,
                "seed": record.spec.seed,
                "statement": statement,
                "evidence_refs": refs,
                "pass_index": None,
            }
        )
    return occurrences


def _inspector_occurrences(record: ExplorationTraceRecord) -> list[dict[str, Any]]:
    contexts = _observation_context(
        record.result.transitions,
        record.result.trace_evidence_refs,
    )
    occurrences: list[dict[str, Any]] = []
    for pass_index, artifact in enumerate(record.inspection_passes, start=1):
        if artifact is None:
            continue
        normalized = normalize_inspection_artifact(artifact)
        seen_in_pass: dict[CandidateKey, dict[str, Any]] = {}
        for finding in normalized["findings"]:
            evidence = _evidence_context(finding.get("evidence_refs"), contexts)
            if evidence is None:
                continue
            refs, phase, event = evidence
            category = finding["kind"]
            if category == "numeric":
                rule = _numeric_rule(finding.get("comparison"))
                field_name = finding.get("field") or ""
            else:
                rule = finding.get("rule") or ""
                field_name = ""
            key = _candidate_key(category, rule, field_name, phase, event)
            existing = seen_in_pass.get(key)
            if existing is not None:
                existing["evidence_refs"] = tuple(
                    dict.fromkeys([*existing["evidence_refs"], *refs])
                )
                statement = str(finding.get("statement") or "").strip()
                if statement and statement not in existing["statement"].split(" | "):
                    merged = " | ".join([existing["statement"], statement])
                    if len(merged) <= MAX_MERGED_FINDING_STATEMENT_CHARS:
                        existing["statement"] = merged
                continue
            occurrence = {
                "key": key,
                "source": "inspector",
                "trace_id": record.opaque_trace_id,
                "mission_id": record.spec.mission_id,
                "seed": record.spec.seed,
                "statement": str(finding.get("statement") or "").strip(),
                "evidence_refs": refs,
                "pass_index": pass_index,
            }
            seen_in_pass[key] = occurrence
            occurrences.append(occurrence)
    return occurrences


def _normalized_invariant_keys(record: ExplorationTraceRecord) -> set[CandidateKey]:
    contexts = _observation_context(
        record.result.transitions,
        record.result.trace_evidence_refs,
    )
    keys: set[CandidateKey] = set()
    for validation in record.result.invariant_validations:
        if not isinstance(validation, dict):
            continue
        if _evidence_context(validation.get("evidence_refs"), contexts) is None:
            continue
        keys.add(
            _candidate_key(
                validation.get("category"),
                validation.get("rule"),
                validation.get("field"),
                validation.get("phase"),
                validation.get("event"),
            )
        )
    return keys


def _runtime_oracle_occurrences(
    record: ExplorationTraceRecord,
) -> list[dict[str, Any]]:
    contexts = _observation_context(
        record.result.transitions,
        record.result.trace_evidence_refs,
    )
    occurrences: list[dict[str, Any]] = []
    for validation in record.result.invariant_validations:
        if not isinstance(validation, dict) or not validation.get("emit_candidate"):
            continue
        evidence = _evidence_context(validation.get("evidence_refs"), contexts)
        if evidence is None:
            continue
        refs, phase, event = evidence
        key = _candidate_key(
            validation.get("category"),
            validation.get("rule"),
            validation.get("field"),
            validation.get("phase") or phase,
            validation.get("event") or event,
        )
        occurrences.append(
            {
                "key": key,
                "source": "runtime_oracle",
                "trace_id": record.opaque_trace_id,
                "mission_id": record.spec.mission_id,
                "seed": record.spec.seed,
                "statement": str(validation.get("statement") or "").strip(),
                "evidence_refs": refs,
                "pass_index": None,
            }
        )
    return occurrences


def _screenshot_retention_axes(
    record: ExplorationTraceRecord,
) -> tuple[str, ...]:
    """Select independent evidence axes without exposing the screenshot to inspectors."""

    axes: list[str] = []
    if _planner_occurrences(record):
        axes.append("planner")
    if _inspector_occurrences(record):
        axes.append("inspector")
    if _runtime_oracle_occurrences(record):
        axes.append("runtime_oracle")
    return tuple(axes)


def _record_failures(record: ExplorationTraceRecord) -> list[dict[str, Any]]:
    common = {
        "trace_id": record.opaque_trace_id,
        "mission_id": record.spec.mission_id,
        "seed": record.spec.seed,
    }
    failures = [{**common, **dict(item)} for item in record.result.harness_failures]
    if record.result.launch_faults:
        failures.append(
            {
                **common,
                "kind": "injected_fault_config",
                "detail": "Track A launch contained a non-empty fault list.",
            }
        )
    if record.result.execution_status != "completed":
        status = _normalize_token(record.result.execution_status)
        classification = f"{status} {_normalize_token(record.result.error)}"
        if "model" in classification or "llm" in classification:
            kind = "model_failure"
        elif "schema" in classification or "contract" in classification:
            kind = "schema_failure"
        elif "bridge" in classification or "infrastructure" in classification:
            kind = "bridge_failure"
        else:
            kind = "harness_failure"
        failures.append(
            {**common, "kind": kind, "detail": record.result.error or status}
        )
    if record.result.coverage_status != "reached":
        failures.append(
            {
                **common,
                "kind": "coverage_failure",
                "detail": record.result.coverage_status,
            }
        )
    if not record.result.transitions:
        failures.append(
            {
                **common,
                "kind": "schema_failure",
                "detail": "completed exploration trace contains no observations",
            }
        )
    valid_passes = sum(artifact is not None for artifact in record.inspection_passes)
    if record.result.transitions and (
        len(record.inspection_passes) != INSPECTION_REPETITIONS
        or valid_passes < INSPECTION_REPETITIONS
    ):
        failures.append(
            {
                **common,
                "kind": "inspection_failure",
                "detail": f"valid_passes={valid_passes}/{INSPECTION_REPETITIONS}",
            }
        )
    return failures


def _candidate_eligible(record: ExplorationTraceRecord) -> bool:
    return (
        not record.result.launch_faults
        and record.result.execution_status == "completed"
        and bool(record.result.transitions)
        and not record.result.harness_failures
        and len(record.inspection_passes) == INSPECTION_REPETITIONS
        and all(artifact is not None for artifact in record.inspection_passes)
    )


def _coverage_eligible(record: ExplorationTraceRecord) -> bool:
    return _candidate_eligible(record) and record.result.coverage_status == "reached"


def _priority(key: CandidateKey, statements: Sequence[str], reproduced: bool) -> str:
    haystack = " ".join([*key, *statements]).lower()
    reason_tokens = set(re.findall(r"[a-z0-9]+", haystack))
    if (
        "crash" in reason_tokens
        or "hang" in reason_tokens
        or "freeze" in reason_tokens
        or {"unrecoverable", "blocker"}.issubset(reason_tokens)
    ):
        return "P0"
    if reproduced and any(
        token in haystack
        for token in (
            "progression",
            "state-loss",
            "state-damage",
            "currency",
            "inventory-loss",
            "upgrade-loss",
            "restart-leak",
        )
    ):
        return "P1"
    if reproduced and any(
        token in haystack
        for token in (
            "numeric",
            "ui",
            "combat",
            "damage",
            "health",
            "display",
            "player_view",
        )
    ):
        return "P2"
    return "P3"


def build_exploration_report(
    records: Sequence[ExplorationTraceRecord],
    *,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate evidence-valid planner and inspector candidates without ground truth."""

    grouped: dict[CandidateKey, list[dict[str, Any]]] = defaultdict(list)
    invariant_keys: set[CandidateKey] = set()
    failures: list[dict[str, Any]] = []
    for record in records:
        failures.extend(_record_failures(record))
        if not _candidate_eligible(record):
            continue
        invariant_keys.update(_normalized_invariant_keys(record))
        grouped_occurrences = [
            *_planner_occurrences(record),
            *_inspector_occurrences(record),
            *_runtime_oracle_occurrences(record),
        ]
        for occurrence in grouped_occurrences:
            grouped[occurrence["key"]].append(occurrence)

    candidates: list[dict[str, Any]] = []
    for key in sorted(grouped):
        occurrences = grouped[key]
        sources = {item["source"] for item in occurrences}
        agent_sources = sources & {"planner", "inspector"}
        surface = (
            "shared"
            if agent_sources == {"planner", "inspector"}
            else "planner_only"
            if agent_sources == {"planner"}
            else "inspector_only"
            if agent_sources == {"inspector"}
            else "runtime_oracle"
        )
        mission_seeds: dict[str, set[int]] = defaultdict(set)
        for occurrence in occurrences:
            mission_seeds[occurrence["mission_id"]].add(occurrence["seed"])
        reproduced = any(len(seeds) >= 2 for seeds in mission_seeds.values())
        agreements: dict[str, str] = {}
        inspector_passes: dict[str, set[int]] = defaultdict(set)
        for occurrence in occurrences:
            if occurrence["source"] == "inspector":
                inspector_passes[occurrence["trace_id"]].add(occurrence["pass_index"])
        for trace_id, passes in sorted(inspector_passes.items()):
            agreements[trace_id] = f"{len(passes)}/{INSPECTION_REPETITIONS}"
        statements = list(
            dict.fromkeys(item["statement"] for item in occurrences if item["statement"])
        )
        evidence = [
            {
                "trace_id": item["trace_id"],
                "mission_id": item["mission_id"],
                "seed": item["seed"],
                "source": item["source"],
                "evidence_refs": list(item["evidence_refs"]),
                **(
                    {"inspection_pass": item["pass_index"]}
                    if item["pass_index"] is not None
                    else {}
                ),
            }
            for item in occurrences
        ]
        candidate_id = _sha256({"candidate_key": key})[:16]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "category": key[0],
                "rule": key[1],
                "field": key[2],
                "phase": key[3],
                "event": key[4],
                "tier": (
                    "validated-invariant candidate"
                    if key in invariant_keys
                    else "evidence-linked candidate"
                ),
                "surface": surface,
                "runtime_oracle_supported": key in invariant_keys,
                "reproduced": reproduced,
                "priority": _priority(key, statements, reproduced),
                "inspection_agreement_by_trace": agreements,
                "missions": sorted(mission_seeds),
                "seeds_by_mission": {
                    mission: sorted(seeds) for mission, seeds in sorted(mission_seeds.items())
                },
                "statements": statements,
                "evidence": evidence,
            }
        )

    surface_counter = Counter(candidate["surface"] for candidate in candidates)
    priority_counter = Counter(candidate["priority"] for candidate in candidates)
    tier_counter = Counter(candidate["tier"] for candidate in candidates)
    eligible_count = sum(_coverage_eligible(record) for record in records)
    candidate_evidence_count = sum(_candidate_eligible(record) for record in records)
    screenshots = [
        {
            "trace_id": record.opaque_trace_id,
            "path": record.result.screenshot_path,
            "retention_axes": list(record.result.screenshot_retention_axes),
            "capture_error": _public_error_type(record.result.screenshot_error),
        }
        for record in records
        if record.result.screenshot_path
        or record.result.screenshot_error
        or record.result.screenshot_retention_axes
    ]
    return {
        "schema_version": EXPLORATION_REPORT_SCHEMA,
        "metadata": dict(metadata),
        "summary": {
            "scheduled_traces": len(records),
            "eligible_gameplay_traces": eligible_count,
            "candidate_evidence_traces": candidate_evidence_count,
            "coverage_not_reached_traces": sum(
                record.result.coverage_status == "not_reached" for record in records
            ),
            "excluded_harness_traces": len(records) - candidate_evidence_count,
            "candidate_count": len(candidates),
            "retained_screenshot_count": sum(
                bool(record.result.screenshot_path) for record in records
            ),
            "capture_error_count": sum(
                bool(record.result.screenshot_error) for record in records
            ),
        },
        "surface_counts": {
            "planner_only": surface_counter["planner_only"],
            "inspector_only": surface_counter["inspector_only"],
            "shared": surface_counter["shared"],
            "runtime_oracle": surface_counter["runtime_oracle"],
            "union": (
                surface_counter["planner_only"]
                + surface_counter["inspector_only"]
                + surface_counter["shared"]
            ),
        },
        "tier_counts": {
            "validated-invariant candidate": tier_counter[
                "validated-invariant candidate"
            ],
            "evidence-linked candidate": tier_counter["evidence-linked candidate"],
        },
        "priority_counts": {
            priority: priority_counter[priority] for priority in ("P0", "P1", "P2", "P3")
        },
        "candidates": candidates,
        "screenshots": screenshots,
        "harness_failures": failures,
        "interpretation": {
            "candidate_scope": "관찰 증거에 연결된 후보이며 확정 판정이 아닙니다.",
            "zero_scope": "후보가 없더라도 게임에 버그가 없음을 의미하지 않습니다.",
        },
    }


def exploration_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _callable_code_digest(function: Any) -> str:
    code = function.__code__
    return _sha256(
        {
            "bytecode": code.co_code.hex(),
            "constants": [repr(value) for value in code.co_consts],
            "names": list(code.co_names),
            "variables": list(code.co_varnames),
        }
    )


@lru_cache(maxsize=1)
def _steering_dependency_source_hashes() -> dict[str, str]:
    """Freeze planner dependency hashes for the lifetime of the loaded evaluator."""

    return {
        name: _file_hash(Path(__file__).with_name(name))
        for name in (
            "charter.py",
            "memory.py",
            "planners.py",
            "reporting.py",
            "run.py",
            "state_channels.py",
        )
    }


def _steering_prompt_digest() -> str:
    """Bind resume identity to the exact planner prompt construction code."""

    return _sha256(
        {
            "version": STEERING_PROMPT_TEMPLATE_VERSION,
            "dependencies": _steering_dependency_source_hashes(),
            "planning_system_prompt": _callable_code_digest(
                LLMPlanner._planning_system_prompt
            ),
            "planning_context_messages": _callable_code_digest(
                LLMPlanner._planning_context_messages
            ),
            "planning_payload": _callable_code_digest(LLMPlanner._planning_payload),
            "decision_schema": _callable_code_digest(build_decision_response_schema),
            "max_tokens": PLANNING_MAX_TOKENS,
            "plan_horizon_seconds": STEERING_PLAN_HORIZON_SECONDS,
        }
    )


def render_exploration_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    surfaces = report.get("surface_counts") or {}
    tiers = report.get("tier_counts") or {}
    priorities = report.get("priority_counts") or {}
    candidates = report.get("candidates") or []
    failures = report.get("harness_failures") or []
    lines = [
        "# 자율 탐색 버그 후보 보고서",
        "",
        "이 보고서는 정상 QA 빌드의 자율 플레이에서 관찰된 증거 기반 후보를 정리합니다. 후보는 확정 판정이 아닙니다.",
        "",
        "## 실행 요약",
        "",
        f"- 예약 trace: {summary.get('scheduled_traces', 0)}",
        f"- coverage 목표 도달 trace: {summary.get('eligible_gameplay_traces', 0)}",
        f"- 후보 증거가 유효한 trace: {summary.get('candidate_evidence_traces', 0)}",
        f"- coverage 미도달 trace: {summary.get('coverage_not_reached_traces', 0)}",
        f"- harness 제외 trace: {summary.get('excluded_harness_traces', 0)}",
        f"- 후보 합계: {summary.get('candidate_count', 0)}",
        f"- 보존 스크린샷: {summary.get('retained_screenshot_count', 0)}",
        f"- 스크린샷 캡처 오류: {summary.get('capture_error_count', 0)}",
        "",
        "## LLM 탐지 표면",
        "",
        f"- planner-only: {surfaces.get('planner_only', 0)}",
        f"- inspector-only: {surfaces.get('inspector_only', 0)}",
        f"- shared: {surfaces.get('shared', 0)}",
        f"- LLM union: {surfaces.get('union', 0)}",
        f"- runtime-oracle-only (별도): {surfaces.get('runtime_oracle', 0)}",
        "",
        "## 후보 등급",
        "",
        f"- validated-invariant candidate: {tiers.get('validated-invariant candidate', 0)}",
        f"- evidence-linked candidate: {tiers.get('evidence-linked candidate', 0)}",
        "",
        "## 우선순위",
        "",
        *[f"- {priority}: {priorities.get(priority, 0)}" for priority in ("P0", "P1", "P2", "P3")],
        "",
        "## 후보",
        "",
    ]
    if not candidates:
        lines.extend(
            [
                "관찰된 버그 후보가 없습니다. 이는 게임에 버그가 없음을 의미하지 않습니다.",
            ]
        )
    else:
        lines.extend(
            [
                "| ID | 등급 | 우선순위 | 표면 | 재현 | 검사 합의 | 규칙 | phase/event |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for candidate in candidates:
            agreements = candidate.get("inspection_agreement_by_trace") or {}
            agreement_label = ", ".join(
                f"`{trace_id}`={agreement}"
                for trace_id, agreement in sorted(agreements.items())
            ) or "-"
            row = {
                **candidate,
                "reproduced_label": "예" if candidate.get("reproduced") else "아니오",
                "agreement_label": agreement_label,
            }
            lines.append(
                "| `{candidate_id}` | {tier} | **{priority}** | {surface} | {reproduced_label} | {agreement_label} | `{rule}` | `{phase}` / `{event}` |".format(
                    **row
                )
            )
        lines.extend(["", "### 후보별 증거", ""])
        for candidate in candidates:
            evidence = list(candidate.get("evidence") or [])
            lines.append(f"- `{candidate.get('candidate_id', '-')}`")
            for item in evidence[:MAX_MARKDOWN_EVIDENCE_ROWS_PER_CANDIDATE]:
                refs = ", ".join(
                    f"`{reference}`" for reference in item.get("evidence_refs") or []
                ) or "-"
                pass_label = (
                    f", 검사 pass {item['inspection_pass']}"
                    if "inspection_pass" in item
                    else ""
                )
                lines.append(
                    f"  - `{item.get('trace_id', '-')}` / {item.get('source', '-')}"
                    f"{pass_label}: {refs}"
                )
            omitted = len(evidence) - MAX_MARKDOWN_EVIDENCE_ROWS_PER_CANDIDATE
            if omitted > 0:
                lines.append(f"  - 추가 증거 {omitted}건은 JSON 보고서에 보존됨")
    lines.extend(["", "## 증거 스크린샷", ""])
    screenshots = report.get("screenshots") or []
    if not screenshots:
        lines.append("보존된 증거 스크린샷이 없습니다.")
    else:
        lines.extend(
            [
                "| Trace | 상대 경로 | 보존 축 | 캡처 오류 |",
                "|---|---|---|---|",
            ]
        )
        for screenshot in screenshots:
            axes = ", ".join(screenshot.get("retention_axes") or []) or "-"
            lines.append(
                f"| `{screenshot.get('trace_id', '-')}` | "
                f"`{screenshot.get('path') or '-'}` | `{axes}` | "
                f"`{screenshot.get('capture_error') or '-'}` |"
            )
    lines.extend(["", "## Harness 및 coverage 실패", ""])
    if not failures:
        lines.append("별도 harness 또는 coverage 실패가 없습니다.")
    else:
        lines.extend(
            [
                "| Trace | 종류 | 상세 |",
                "|---|---|---|",
            ]
        )
        for failure in failures:
            detail = str(failure.get("detail") or "").replace("|", "\\|")
            lines.append(
                f"| `{failure.get('trace_id', '-')}` | `{failure.get('kind', 'harness_failure')}` | {detail} |"
            )
    lines.extend(
        [
            "",
            "## 해석 제한",
            "",
            "후보 수는 정답 대비 탐지 지표가 아니며, 관찰되지 않은 문제가 없다는 뜻도 아닙니다.",
        ]
    )
    return "\n".join(lines) + "\n"


def _campaign_hash(config: ExplorationCampaignConfig, build_hash: str) -> str:
    schedule = build_track_a_schedule(config.profile)
    return _sha256(
        {
            "build_hash": build_hash,
            "build_path": str(config.build.resolve()),
            "project_root": str(config.project_root.resolve()),
            "effective_api_endpoint": _sha256(resolve_llm_api_url(config.api_url)),
            "headless": config.headless,
            "quiet": config.quiet,
            "profile": config.profile,
            "steering_model": STEERING_MODEL,
            "steering_prompt_version": STEERING_PROMPT_TEMPLATE_VERSION,
            "steering_prompt": _steering_prompt_digest(),
            "steering_plan_horizon_seconds": STEERING_PLAN_HORIZON_SECONDS,
            "inspector_model": INSPECTOR_MODEL,
            "inspector_effort": INSPECTOR_EFFORT,
            "inspector_prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
            "inspection_normalizer": INSPECTION_NORMALIZER_VERSION,
            "inspection_request_contract_version": INSPECTION_REQUEST_CONTRACT_VERSION,
            "inspection_request_contract": _inspection_request_contract_digest(),
            "rubric": _file_hash(
                config.project_root.resolve() / "config" / "qa-detection-rubric.json"
            ),
            "missions": [asdict(mission) for mission in MISSIONS],
            "seeds": SEEDS,
            "selected_schedule": [asdict(spec) for spec in schedule],
            "inspection_repetitions": INSPECTION_REPETITIONS,
            "logical_call_cap": LOGICAL_INSPECTION_CALL_CAP,
            "faults": [],
        }
    )


def _episode_hash(campaign_hash: str, spec: ExplorationEpisodeSpec) -> str:
    return _sha256({"campaign_hash": campaign_hash, "spec": asdict(spec)})


def _exploration_episode_artifacts(
    output: Path,
    episode_path: Path,
    launch_path: Path,
) -> list[Path]:
    artifacts = [episode_path, launch_path]
    if episode_path.is_file():
        screenshot_path = _read_json_object(episode_path).get("screenshot_path")
        if isinstance(screenshot_path, str) and screenshot_path.startswith("screenshots/"):
            artifacts.append(output / screenshot_path)
    return artifacts


def _run_episode(
    *,
    spec: ExplorationEpisodeSpec,
    output: Path,
    backend: ExplorationBackend,
    checkpoint: CheckpointStore,
    campaign_hash: str,
) -> tuple[ExplorationEpisodeResult, bool]:
    validate_track_a_spec(spec)
    directory = output / "traces" / spec.mission_id / str(spec.seed)
    path = directory / "episode-result.json"
    launch_path = directory / "launch-manifest.json"
    input_hash = _episode_hash(campaign_hash, spec)
    artifacts = _exploration_episode_artifacts(output, path, launch_path)
    if checkpoint.reusable(spec.unit_id, input_hash, artifacts):
        return ExplorationEpisodeResult.from_dict(_read_json_object(path)), True
    checkpoint.mark_started(spec.unit_id, input_hash)
    _atomic_write_json(
        launch_path,
        {
            "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
            "track": "A",
            "scope": "episode",
            "campaign_hash": campaign_hash,
            "unit_id": spec.unit_id,
            "mission_id": spec.mission_id,
            "seed": spec.seed,
            "launch": {
                "faults": [],
                "policy": "llm",
                "source_tools_enabled": False,
                "nested_inspector_enabled": False,
            },
        },
    )
    try:
        result = backend.run_autonomous(spec, path.parent)
    except Exception as error:
        result = ExplorationEpisodeResult(
            transitions=[],
            execution_status="infrastructure_error",
            coverage_status="error",
            error=_sanitize_error_type(type(error).__name__) or "Exception",
        )
    _atomic_write_json(path, result.to_dict())
    if (
        result.execution_status == "completed"
        and result.coverage_status in {"reached", "not_reached"}
        and not result.launch_faults
        and bool(result.transitions)
        and not result.harness_failures
    ):
        checkpoint.mark_complete(spec.unit_id, input_hash, [path, launch_path])
    else:
        checkpoint.mark_failed(
            spec.unit_id,
            input_hash,
            result.error or result.coverage_status or result.execution_status,
        )
    return result, False


def _finalize_exploration_screenshot(
    *,
    record: ExplorationTraceRecord,
    output: Path,
    checkpoint: CheckpointStore,
    campaign_hash: str,
) -> ExplorationTraceRecord:
    directory = output / "traces" / record.spec.mission_id / str(record.spec.seed)
    episode_path = directory / "episode-result.json"
    launch_path = directory / "launch-manifest.json"
    finalized = finalize_evidence_screenshot(
        campaign_root=output,
        trace_output_dir=directory,
        opaque_trace_id=record.opaque_trace_id,
        screenshot_path=record.result.screenshot_path,
        screenshot_error=record.result.screenshot_error,
        retention_axes=_screenshot_retention_axes(record),
    )
    result = replace(
        record.result,
        screenshot_path=finalized.path,
        screenshot_error=finalized.error,
        screenshot_retention_axes=finalized.retention_axes,
    )
    _atomic_write_json(episode_path, result.to_dict())
    artifacts = [episode_path, launch_path]
    if finalized.path:
        artifacts.append(output / finalized.path)
    checkpoint.mark_complete(
        record.spec.unit_id,
        _episode_hash(campaign_hash, record.spec),
        artifacts,
    )
    return replace(record, result=result)


def _validate_required_episode_result(
    spec: ExplorationEpisodeSpec,
    result: ExplorationEpisodeResult,
) -> None:
    if result.launch_faults:
        raise CampaignContractError(
            f"Track A clean-launch contract failed for {spec.unit_id}"
        )
    if result.execution_status != "completed":
        raise CampaignExecutionError(
            f"required exploration execution failed for {spec.unit_id}: "
            f"{result.execution_status}"
        )
    if result.harness_failures:
        raise CampaignExecutionError(
            f"required exploration harness contract failed for {spec.unit_id}"
        )
    if not result.transitions:
        raise CampaignExecutionError(
            f"required exploration trace is empty for {spec.unit_id}"
        )
    if result.coverage_status not in {"reached", "not_reached"}:
        raise CampaignExecutionError(
            f"required exploration coverage contract failed for {spec.unit_id}"
        )


def _inspect_record(
    *,
    opaque_trace_id: str,
    result: ExplorationEpisodeResult,
    output: Path,
    inspector: InspectorAdapter,
    checkpoint: CheckpointStore,
    budget: InspectionCallBudget,
    campaign_hash: str,
) -> list[dict[str, Any] | None]:
    if not result.transitions:
        return [None] * INSPECTION_REPETITIONS
    input_hash = _sha256(
        {
            "campaign_hash": campaign_hash,
            "opaque_trace_id": opaque_trace_id,
            "transitions": list(result.transitions),
        }
    )
    passes: list[dict[str, Any] | None] = []
    for pass_index in range(1, INSPECTION_REPETITIONS + 1):
        try:
            passes.append(
                inspect_trace_pass(
                    trace_id=opaque_trace_id,
                    pass_index=pass_index,
                    transitions=result.transitions,
                    output_dir=output / "inspections" / opaque_trace_id,
                    inspector=inspector,
                    checkpoint=checkpoint,
                    budget=budget,
                    input_hash=input_hash,
                )
            )
        except InspectionPassFailure:
            passes.append(None)
    return passes


def _write_running_manifest(
    output: Path,
    campaign_hash: str,
    build_hash: str,
    config: ExplorationCampaignConfig,
) -> None:
    _atomic_write_json(
        output / "campaign-manifest.json",
        {
            "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
            "campaign_hash": campaign_hash,
            "status": "running",
            "track": "A",
            "profile": config.profile,
            "hashes": {
                "build": build_hash,
                "config": campaign_hash,
                "api_endpoint": _public_api_endpoint_hash(config.api_url),
                "steering_prompt": _steering_prompt_digest(),
                "inspection_normalizer": _sha256(INSPECTION_NORMALIZER_VERSION),
                "inspection_request_contract": _inspection_request_contract_digest(),
            },
            "steering_prompt_version": STEERING_PROMPT_TEMPLATE_VERSION,
            "inspection_request_contract_version": (
                INSPECTION_REQUEST_CONTRACT_VERSION
            ),
        },
    )


def _archive_published_results(output: Path) -> None:
    sources = (
        output / "campaign-manifest.json",
        output / "exploration-report.json",
        output / "exploration-report.ko.md",
    )
    existing = [source for source in sources if source.exists()]
    if not existing:
        return
    archive = output / ".superseded-results" / uuid.uuid4().hex
    for source in existing:
        destination = archive / source.relative_to(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)


def _has_published_results(output: Path) -> bool:
    return any(
        (output / name).exists()
        for name in (
            "campaign-manifest.json",
            "exploration-report.json",
            "exploration-report.ko.md",
        )
    )


def _cleanup_published_results(output: Path) -> str | None:
    """Archive public results or invalidate adjacent reports before failure publication."""

    try:
        _archive_published_results(output)
        return None
    except OSError as archive_error:
        cleanup_error: OSError = archive_error
        for name in ("exploration-report.json", "exploration-report.ko.md"):
            try:
                (output / name).unlink(missing_ok=True)
            except OSError as unlink_error:
                cleanup_error = unlink_error
        return _sanitize_error_type(type(cleanup_error).__name__) or "OSError"


def _write_incomplete_manifest(
    *,
    output: Path,
    campaign_hash: str,
    build_hash: str,
    config: ExplorationCampaignConfig,
    checkpoint: CheckpointStore,
    failure_status: str,
    error_type: str,
) -> Path:
    manifest_path = output / "campaign-manifest.json"
    _atomic_write_json(
        manifest_path,
        {
            "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
            "campaign_hash": campaign_hash,
            "status": "incomplete",
            "track": "A",
            "profile": config.profile,
            "hashes": {
                "build": build_hash,
                "config": campaign_hash,
                "api_endpoint": _public_api_endpoint_hash(config.api_url),
                "steering_prompt": _steering_prompt_digest(),
                "inspection_normalizer": _sha256(INSPECTION_NORMALIZER_VERSION),
                "inspection_request_contract": _inspection_request_contract_digest(),
            },
            "steering_prompt_version": STEERING_PROMPT_TEMPLATE_VERSION,
            "inspection_request_contract_version": (
                INSPECTION_REQUEST_CONTRACT_VERSION
            ),
            "counts": {
                "logical_inspection_calls": checkpoint.logical_calls,
                "http_attempts": checkpoint.http_attempts,
            },
            "failure": {
                "status": failure_status,
                "error_type": error_type,
            },
        },
    )
    return manifest_path


def record_exploration_initialization_failure(
    config: ExplorationCampaignConfig,
    error: Exception,
) -> Path:
    """Publish a safe incomplete Track A manifest before gameplay starts."""

    build_hash = hash_path(config.build)
    campaign_hash = _campaign_hash(config, build_hash)
    output = config.output.resolve()
    if output.exists() and any(output.iterdir()) and not config.resume:
        raise CampaignContractError("non-empty exploration output requires --resume")
    checkpoint = _prepare_campaign_output(output, campaign_hash)
    try:
        if _has_published_results(output):
            _write_running_manifest(output, campaign_hash, build_hash, config)
        _archive_published_results(output)
    except OSError:
        cleanup_error = _cleanup_published_results(output) or "OSError"
        _write_incomplete_manifest(
            output=output,
            campaign_hash=campaign_hash,
            build_hash=build_hash,
            config=config,
            checkpoint=checkpoint,
            failure_status="publication_cleanup_failure",
            error_type=cleanup_error,
        )
        raise
    return _write_incomplete_manifest(
        output=output,
        campaign_hash=campaign_hash,
        build_hash=build_hash,
        config=config,
        checkpoint=checkpoint,
        failure_status="model_failure",
        error_type=_sanitize_error_type(type(error).__name__) or "Exception",
    )


def run_exploration_campaign(
    config: ExplorationCampaignConfig,
    *,
    backend: ExplorationBackend,
    inspector: InspectorAdapter,
) -> ExplorationCampaignResult:
    """Run or exactly resume the fixed clean-build Track A campaign."""

    build_hash = hash_path(config.build)
    campaign_hash = _campaign_hash(config, build_hash)
    schedule = build_track_a_schedule(config.profile)
    planned_inspection_calls = _planned_track_a_inspection_calls(schedule)
    output = config.output.resolve()
    if output.exists() and any(output.iterdir()) and not config.resume:
        raise CampaignContractError("non-empty exploration output requires --resume")
    checkpoint = _prepare_campaign_output(output, campaign_hash)
    resumed = bool(checkpoint.units)
    budget = InspectionCallBudget(
        cap=LOGICAL_INSPECTION_CALL_CAP,
        logical_calls=checkpoint.logical_calls,
        http_attempts=checkpoint.http_attempts,
    )
    records: list[ExplorationTraceRecord] = []
    trace_manifest: list[dict[str, Any]] = []
    publication_phase = "archive"
    try:
        if _has_published_results(output):
            _write_running_manifest(output, campaign_hash, build_hash, config)
        _archive_published_results(output)
        _write_running_manifest(output, campaign_hash, build_hash, config)
        publication_phase = "campaign"
        for spec in schedule:
            result, episode_resumed = _run_episode(
                spec=spec,
                output=output,
                backend=backend,
                checkpoint=checkpoint,
                campaign_hash=campaign_hash,
            )
            opaque_id = _opaque_trace_id(campaign_hash, spec.unit_id)
            if not episode_resumed:
                _invalidate_trace_inspections(
                    checkpoint=checkpoint,
                    output=output,
                    opaque_trace_id=opaque_id,
                )
            _validate_required_episode_result(spec, result)
            passes = _inspect_record(
                opaque_trace_id=opaque_id,
                result=result,
                output=output,
                inspector=inspector,
                checkpoint=checkpoint,
                budget=budget,
                campaign_hash=campaign_hash,
            )
            if (
                len(passes) != INSPECTION_REPETITIONS
                or any(artifact is None for artifact in passes)
            ):
                raise CampaignExecutionError(
                    f"required exploration inspection failed for {spec.unit_id}"
                )
            record = ExplorationTraceRecord(
                spec=spec,
                opaque_trace_id=opaque_id,
                result=result,
                inspection_passes=tuple(passes),
                resumed=episode_resumed,
            )
            record = _finalize_exploration_screenshot(
                record=record,
                output=output,
                checkpoint=checkpoint,
                campaign_hash=campaign_hash,
            )
            records.append(record)
            trace_manifest.append(
                {
                    "unit_id": spec.unit_id,
                    "opaque_trace_id": opaque_id,
                    "mission_id": spec.mission_id,
                    "tier": spec.tier,
                    "seed": spec.seed,
                    "driver": spec.driver,
                    "model": spec.model,
                    "source_tools_enabled": spec.source_tools_enabled,
                    "nested_inspector_enabled": spec.nested_inspector_enabled,
                    "launch": {"variant": "clean", "faults": list(result.launch_faults)},
                    "execution_status": result.execution_status,
                    "coverage_status": result.coverage_status,
                    "valid_inspection_passes": sum(item is not None for item in passes),
                    "screenshot_path": record.result.screenshot_path,
                    "screenshot_error": _public_error_type(
                        record.result.screenshot_error
                    ),
                    "screenshot_retention_axes": list(
                        record.result.screenshot_retention_axes
                    ),
                    "resumed": episode_resumed,
                }
            )
        report = build_exploration_report(
            records,
            metadata={
                "campaign_id": campaign_hash[:16],
                "track": "A",
                "profile": config.profile,
                "build_hash": build_hash,
                "steering_model": STEERING_MODEL,
                "inspection_model": INSPECTOR_MODEL,
            },
        )
        report_path = output / "exploration-report.json"
        markdown_path = output / "exploration-report.ko.md"
        manifest_path = output / "campaign-manifest.json"
        publication_phase = "report"
        report_path.write_text(exploration_report_json(report), encoding="utf-8")
        markdown_path.write_text(render_exploration_markdown(report), encoding="utf-8")
        _atomic_write_json(
            manifest_path,
            {
                "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
                "campaign_hash": campaign_hash,
                "status": "complete",
                "track": "A",
                "profile": config.profile,
                "hashes": {
                    "build": build_hash,
                    "config": campaign_hash,
                    "steering_model": _sha256(STEERING_MODEL),
                    "inspector_model": _sha256(INSPECTOR_MODEL),
                    "inspector_prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
                    "steering_prompt": _steering_prompt_digest(),
                    "inspection_normalizer": _sha256(
                        INSPECTION_NORMALIZER_VERSION
                    ),
                    "inspection_request_contract": _inspection_request_contract_digest(),
                    "rubric": _file_hash(
                        config.project_root / "config" / "qa-detection-rubric.json"
                    ),
                    "api_endpoint": _public_api_endpoint_hash(config.api_url),
                },
                "steering_prompt_version": STEERING_PROMPT_TEMPLATE_VERSION,
                "inspection_request_contract_version": (
                    INSPECTION_REQUEST_CONTRACT_VERSION
                ),
                "models": {
                    "steering": STEERING_MODEL,
                    "inspection": INSPECTOR_MODEL,
                    "inspection_effort": INSPECTOR_EFFORT,
                },
                "limits": {
                    "inspection_repetitions": INSPECTION_REPETITIONS,
                    "planned_inspection_calls": planned_inspection_calls,
                    "logical_inspection_call_cap": LOGICAL_INSPECTION_CALL_CAP,
                },
                "counts": {
                    "scheduled_traces": len(records),
                    "eligible_gameplay_traces": report["summary"][
                        "eligible_gameplay_traces"
                    ],
                    "candidate_evidence_traces": report["summary"][
                        "candidate_evidence_traces"
                    ],
                    "coverage_not_reached_traces": report["summary"][
                        "coverage_not_reached_traces"
                    ],
                    "candidate_count": report["summary"]["candidate_count"],
                    "retained_screenshot_count": report["summary"][
                        "retained_screenshot_count"
                    ],
                    "capture_error_count": report["summary"][
                        "capture_error_count"
                    ],
                    "logical_inspection_calls": checkpoint.logical_calls,
                    "http_attempts": checkpoint.http_attempts,
                },
                "traces": trace_manifest,
                "reports": {
                    "json": report_path.name,
                    "markdown": markdown_path.name,
                },
            },
        )
        publication_phase = "complete"
        return ExplorationCampaignResult(
            manifest_path=manifest_path,
            report_path=report_path,
            records=tuple(records),
            resumed=resumed,
        )
    except Exception as error:
        cleanup_error = _cleanup_published_results(output)
        publication_failure = cleanup_error is not None or publication_phase in {
            "archive",
            "report",
        }
        _write_incomplete_manifest(
            output=output,
            campaign_hash=campaign_hash,
            build_hash=build_hash,
            config=config,
            checkpoint=checkpoint,
            failure_status=(
                "publication_cleanup_failure"
                if publication_failure
                else "execution_failure"
            ),
            error_type=(
                cleanup_error
                or _sanitize_error_type(type(error).__name__)
                or "Exception"
            ),
        )
        raise


def _mission_coverage_status(
    mission_id: str,
    transitions: Sequence[dict[str, Any]],
) -> str:
    observations = [
        item.get("observation")
        for item in transitions
        if isinstance(item.get("observation"), dict)
    ]
    if not observations:
        return "not_reached"
    phases = {_normalize_token(item.get("phase")) for item in observations}
    decisions = [
        item.get("decision")
        for item in transitions
        if isinstance(item.get("decision"), dict)
    ]
    levels = [
        int((item.get("player") or {}).get("level") or 0)
        for item in observations
        if isinstance(item.get("player"), dict)
    ]
    progress = [item.get("progress") or {} for item in observations]
    has_upgrade = any(item.get("action") == "select_upgrade" for item in decisions)
    has_restart = any(item.get("action") == "restart" for item in decisions)
    has_item_surface = any(
        bool(item.get("inventory"))
        or "chest" in str(item.get("event_state") or {}).lower()
        or "item" in str(item.get("event_state") or {}).lower()
        for item in observations
    )
    if mission_id == "core-menu-level1":
        reached = "active_gameplay" in phases
    elif mission_id == "core-combat-survival":
        reached = "active_gameplay" in phases and any(
            float(item.get("level_time") or 0.0) > 0 for item in progress
        )
    elif mission_id in {
        "core-progression-upgrades",
        "long-multi-level-growth-upgrades",
    }:
        reached = max(levels or [0]) > 1 or has_upgrade
    elif mission_id in {
        "core-items-chests-inventory",
        "long-items-chests-area-effects",
    }:
        reached = has_item_surface
    elif mission_id == "core-death-restart-isolation":
        reached = "game_over" in phases and has_restart
    else:
        reached = "active_gameplay" in phases and any(
            float(item.get("damage_dealt") or 0.0) > 0 for item in progress
        )
    return "reached" if reached else "not_reached"


def _terminal_reason(kind: str, severity: str) -> str | None:
    tokens = set(re.findall(r"[a-z0-9]+", kind))
    if "crash" in tokens:
        return "crash"
    if "hang" in tokens or "freeze" in tokens:
        return "hang"
    if severity == "critical" and "timeout" in tokens:
        return "hang"
    if {"unrecoverable", "blocker"}.issubset(tokens):
        return "unrecoverable-blocker"
    return None


def _fatal_execution_status(error_type: str, fatal_error: str) -> str:
    normalized_type = error_type.lower()
    if "bridge" in normalized_type:
        return "bridge_failure"
    if any(
        token in normalized_type
        for token in (
            "llmcontract",
            "evaluationcontract",
            "scenariocontract",
            "schema",
            "jsondecode",
            "validationerror",
        )
    ):
        return "schema_failure"
    if any(
        token in normalized_type
        for token in ("llm", "provider", "openai", "model")
    ):
        return "model_failure"
    if normalized_type == "valueerror" and any(
        marker in fatal_error.lower()
        for marker in ("llm policy", "qa_api_key", "openai_api_key", "provider")
    ):
        return "model_failure"
    return "infrastructure_failure"


def _anomaly_artifacts(
    spec: ExplorationEpisodeSpec,
    transitions: Sequence[dict[str, Any]],
    report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    validations: list[dict[str, Any]] = []
    harness: list[dict[str, Any]] = []
    trace_evidence_refs: list[str] = []
    for anomaly in report.get("rule_based_anomalies") or []:
        if not isinstance(anomaly, dict):
            continue
        kind = _normalize_token(anomaly.get("kind"))
        if kind.startswith("llm_"):
            harness.append(
                {"kind": "model_failure", "detail": "LLM runtime anomaly"}
            )
            continue
        if kind == "bridge_action_failed":
            harness.append(
                {"kind": "bridge_failure", "detail": "bridge action failed"}
            )
            continue
        try:
            step = int(anomaly.get("step", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= step <= len(transitions)):
            continue
        if kind == "error" and step == len(transitions):
            harness.append(
                {"kind": "bridge_failure", "detail": "player termination error"}
            )
            continue
        transition_index = min(step, len(transitions) - 1)
        observation_value = (
            transitions[transition_index].get("observation") or {}
            if transition_index >= 0
            else {}
        )
        observation_id = str(observation_value.get("observation_id") or "")
        if not observation_id and step == len(transitions):
            for transition in reversed(transitions):
                candidate_observation = transition.get("observation") or {}
                candidate_id = str(candidate_observation.get("observation_id") or "")
                if candidate_id:
                    observation_value = candidate_observation
                    observation_id = candidate_id
                    break
        if not observation_id:
            observation_id = f"trace-termination-{spec.mission_id}-{spec.seed}"
            trace_evidence_refs.append(observation_id)
        field_name = ""
        category = "behavior"
        rule = kind
        emit_candidate = False
        if kind == "view_state_match":
            category = "numeric"
            rule = "numeric-not-equal"
            field_name = "player_view.health_ratio"
        else:
            failure_reason = _terminal_reason(
                kind,
                _normalize_token(anomaly.get("severity")),
            )
            if failure_reason is not None:
                rule = failure_reason
                emit_candidate = True
            elif step == len(transitions):
                continue
        validations.append(
            {
                "category": category,
                "rule": rule,
                "field": field_name,
                "phase": _normalize_token(observation_value.get("phase") or "unknown"),
                "event": _normalize_token(
                    (observation_value.get("event_state") or {}).get("type") or ""
                ),
                "evidence_refs": [observation_id],
                "statement": str(anomaly.get("evidence") or "").strip(),
                "emit_candidate": emit_candidate,
            }
        )
    return validations, harness, list(dict.fromkeys(trace_evidence_refs))


class BridgeExplorationBackend:
    """Run fixed pure-LLM Track A missions through the existing QA Bridge session."""

    def __init__(
        self,
        config: ExplorationCampaignConfig,
        *,
        parse_run_arguments=parse_run_args,
        run_session_fn=run_session,
    ) -> None:
        self.config = config
        self.parse_run_arguments = parse_run_arguments
        self.run_session_fn = run_session_fn

    def run_autonomous(
        self,
        spec: ExplorationEpisodeSpec,
        output_dir: Path,
    ) -> ExplorationEpisodeResult:
        validate_track_a_spec(spec)
        arguments = [
            "--game-exe",
            str(self.config.build),
            "--project-root",
            str(self.config.project_root),
            "--output",
            str(output_dir),
            "--mode",
            "qa",
            "--policy",
            "llm",
            "--model",
            STEERING_MODEL,
            "--objective",
            spec.goal,
            "--bridge-scenario-id",
            spec.mission_id,
            "--seed",
            str(spec.seed),
            "--max-simulation-seconds",
            str(spec.max_simulation_seconds),
            "--max-steps",
            str(spec.max_steps),
            "--max-source-steps",
            "0",
            "--plan-horizon-seconds",
            str(STEERING_PLAN_HORIZON_SECONDS),
            "--inspector-model",
            "",
            "--capture-final-screenshot",
        ]
        if self.config.api_url:
            arguments.extend(["--api-url", self.config.api_url])
        if self.config.headless:
            arguments.append("--headless")
        if self.config.quiet:
            arguments.append("--quiet")
        args = self.parse_run_arguments(arguments)
        args.preset = spec.preset
        args.fault = ""
        self.run_session_fn(args)
        return self._load_result(spec, output_dir)

    @staticmethod
    def _load_result(
        spec: ExplorationEpisodeSpec,
        output_dir: Path,
    ) -> ExplorationEpisodeResult:
        final_screenshot = read_final_screenshot_metadata(output_dir, required=True)
        try:
            report = _read_json_object(output_dir / "report.json")
        except (CampaignContractError, OSError, json.JSONDecodeError, ValueError) as error:
            return ExplorationEpisodeResult(
                transitions=[],
                execution_status="schema_error",
                coverage_status="error",
                error=_sanitize_error_type(type(error).__name__) or "Exception",
                screenshot_path=final_screenshot.path,
                screenshot_error=final_screenshot.error,
            )
        fatal_error = str(report.get("fatal_error") or "").strip()
        if fatal_error:
            raw_error_type = fatal_error.partition(":")[0].strip()
            error_type = _sanitize_error_type(raw_error_type) or "Exception"
            return ExplorationEpisodeResult(
                transitions=[],
                execution_status=_fatal_execution_status(error_type, fatal_error),
                coverage_status="error",
                error=error_type,
                screenshot_path=final_screenshot.path,
                screenshot_error=final_screenshot.error,
            )
        try:
            transitions = [
                json.loads(line)
                for line in (output_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if any(not isinstance(item, dict) for item in transitions):
                raise ValueError("steps.jsonl must contain JSON objects")
            verdict = _read_json_object(output_dir / "verdict.json")
            run = _read_json_object(output_dir / "run.json")
        except (CampaignContractError, OSError, json.JSONDecodeError, ValueError) as error:
            return ExplorationEpisodeResult(
                transitions=[],
                execution_status="schema_error",
                coverage_status="error",
                error=_sanitize_error_type(type(error).__name__) or "Exception",
                screenshot_path=final_screenshot.path,
                screenshot_error=final_screenshot.error,
            )
        arguments = run.get("arguments") or {}
        launch_fault = str(arguments.get("fault") or "").strip()
        validations, harness, trace_evidence_refs = _anomaly_artifacts(
            spec,
            transitions,
            report,
        )
        return ExplorationEpisodeResult(
            transitions=transitions,
            execution_status=str(verdict.get("execution_status") or "infrastructure_error"),
            coverage_status=_mission_coverage_status(spec.mission_id, transitions),
            launch_faults=(launch_fault,) if launch_fault else (),
            invariant_validations=tuple(validations),
            harness_failures=tuple(harness),
            trace_evidence_refs=tuple(trace_evidence_refs),
            error="",
            screenshot_path=final_screenshot.path,
            screenshot_error=final_screenshot.error,
        )


__all__ = [
    "BridgeExplorationBackend",
    "EXPLORATION_REPORT_SCHEMA",
    "ExplorationCampaignConfig",
    "ExplorationCampaignResult",
    "ExplorationEpisodeResult",
    "ExplorationEpisodeSpec",
    "ExplorationTraceRecord",
    "build_exploration_report",
    "build_track_a_schedule",
    "exploration_report_json",
    "render_exploration_markdown",
    "record_exploration_initialization_failure",
    "run_exploration_campaign",
    "validate_track_a_spec",
]
