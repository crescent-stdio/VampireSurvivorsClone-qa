"""Track A clean-build autonomous exploration and evidence-linked candidate reports."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from .detection_campaign import (
    CAMPAIGN_MANIFEST_SCHEMA,
    INSPECTION_REPETITIONS,
    INSPECTOR_EFFORT,
    INSPECTOR_MODEL,
    LOGICAL_INSPECTION_CALL_CAP,
    SEEDS,
    STEERING_MODEL,
    CampaignContractError,
    CheckpointStore,
    InspectionCallBudget,
    InspectorAdapter,
    _atomic_write_json,
    _file_hash,
    _invalidate_trace_inspections,
    _max_chunks_for_steps,
    _opaque_trace_id,
    _prepare_campaign_output,
    _public_api_endpoint_hash,
    _read_json_object,
    _sha256,
    hash_path,
    inspect_trace_pass,
)
from .inspector import INSPECTOR_SYSTEM_PROMPT, normalize_inspection_artifact
from .run import parse_args as parse_run_args
from .run import run_session


EXPLORATION_REPORT_SCHEMA = "qa-exploration-report/v1"
TRACK_A_PRESET = "smoke"
PLANNED_INSPECTION_CALLS = 189


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
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transitions": list(self.transitions),
            "execution_status": self.execution_status,
            "coverage_status": self.coverage_status,
            "launch_faults": list(self.launch_faults),
            "invariant_validations": list(self.invariant_validations),
            "harness_failures": list(self.harness_failures),
            "error": self.error,
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
        return cls(
            transitions=transitions,
            execution_status=str(value.get("execution_status") or "schema_error"),
            coverage_status=str(value.get("coverage_status") or "error"),
            launch_faults=tuple(launch_faults),
            invariant_validations=tuple(validations),
            harness_failures=tuple(failures),
            error=str(value.get("error") or ""),
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


@dataclass(frozen=True)
class ExplorationCampaignResult:
    manifest_path: Path
    report_path: Path
    records: tuple[ExplorationTraceRecord, ...]
    resumed: bool


def build_track_a_schedule() -> tuple[ExplorationEpisodeSpec, ...]:
    """Build the fixed five-core plus three-long mission schedule."""

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
        for mission in MISSIONS
        for seed in SEEDS
    )
    planned = sum(
        _max_chunks_for_steps(spec.max_steps) * INSPECTION_REPETITIONS
        for spec in schedule
    )
    if len(schedule) != 24 or planned != PLANNED_INSPECTION_CALLS:
        raise CampaignContractError("Track A schedule no longer matches fixed acceptance totals")
    for spec in schedule:
        validate_track_a_spec(spec)
    return schedule


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


def _observation_context(
    transitions: Sequence[dict[str, Any]],
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
        _normalize_token(field_name),
        _normalize_token(phase) or "unknown",
        _normalize_token(event),
    )


def _planner_occurrences(record: ExplorationTraceRecord) -> list[dict[str, Any]]:
    contexts = _observation_context(record.result.transitions)
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
        key = _candidate_key("behavior", rule, "", phase, event)
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
    contexts = _observation_context(record.result.transitions)
    occurrences: list[dict[str, Any]] = []
    for pass_index, artifact in enumerate(record.inspection_passes, start=1):
        if artifact is None:
            continue
        normalized = normalize_inspection_artifact(artifact)
        seen_in_pass: set[CandidateKey] = set()
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
            if key in seen_in_pass:
                continue
            seen_in_pass.add(key)
            occurrences.append(
                {
                    "key": key,
                    "source": "inspector",
                    "trace_id": record.opaque_trace_id,
                    "mission_id": record.spec.mission_id,
                    "seed": record.spec.seed,
                    "statement": str(finding.get("statement") or "").strip(),
                    "evidence_refs": refs,
                    "pass_index": pass_index,
                }
            )
    return occurrences


def _normalized_invariant_keys(record: ExplorationTraceRecord) -> set[CandidateKey]:
    contexts = _observation_context(record.result.transitions)
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


def _eligible(record: ExplorationTraceRecord) -> bool:
    return (
        not record.result.launch_faults
        and record.result.execution_status == "completed"
        and record.result.coverage_status == "reached"
        and bool(record.result.transitions)
        and not record.result.harness_failures
        and len(record.inspection_passes) == INSPECTION_REPETITIONS
        and all(artifact is not None for artifact in record.inspection_passes)
    )


def _priority(key: CandidateKey, statements: Sequence[str], reproduced: bool) -> str:
    haystack = " ".join([*key, *statements]).lower()
    if any(token in haystack for token in ("crash", "hang", "freeze", "unrecoverable")):
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
        if not _eligible(record):
            continue
        invariant_keys.update(_normalized_invariant_keys(record))
        grouped_occurrences = [
            *_planner_occurrences(record),
            *_inspector_occurrences(record),
        ]
        for occurrence in grouped_occurrences:
            grouped[occurrence["key"]].append(occurrence)

    candidates: list[dict[str, Any]] = []
    for key in sorted(grouped):
        occurrences = grouped[key]
        sources = {item["source"] for item in occurrences}
        surface = (
            "shared"
            if sources == {"planner", "inspector"}
            else "planner_only"
            if sources == {"planner"}
            else "inspector_only"
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
    eligible_count = sum(_eligible(record) for record in records)
    return {
        "schema_version": EXPLORATION_REPORT_SCHEMA,
        "metadata": dict(metadata),
        "summary": {
            "scheduled_traces": len(records),
            "eligible_gameplay_traces": eligible_count,
            "excluded_harness_traces": len(records) - eligible_count,
            "candidate_count": len(candidates),
        },
        "surface_counts": {
            "planner_only": surface_counter["planner_only"],
            "inspector_only": surface_counter["inspector_only"],
            "shared": surface_counter["shared"],
            "union": len(candidates),
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
        "harness_failures": failures,
        "interpretation": {
            "candidate_scope": "관찰 증거에 연결된 후보이며 확정 판정이 아닙니다.",
            "zero_scope": "후보가 없더라도 게임에 버그가 없음을 의미하지 않습니다.",
        },
    }


def exploration_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_exploration_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    surfaces = report.get("surface_counts") or {}
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
        f"- 후보 집계 대상 trace: {summary.get('eligible_gameplay_traces', 0)}",
        f"- harness/coverage 제외 trace: {summary.get('excluded_harness_traces', 0)}",
        f"- 후보 합계: {summary.get('candidate_count', 0)}",
        "",
        "## 탐지 표면",
        "",
        f"- planner-only: {surfaces.get('planner_only', 0)}",
        f"- inspector-only: {surfaces.get('inspector_only', 0)}",
        f"- shared: {surfaces.get('shared', 0)}",
        f"- union: {surfaces.get('union', 0)}",
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
                "| ID | 등급 | 우선순위 | 표면 | 재현 | 규칙 | phase/event |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for candidate in candidates:
            row = {
                **candidate,
                "reproduced_label": "예" if candidate.get("reproduced") else "아니오",
            }
            lines.append(
                "| `{candidate_id}` | {tier} | **{priority}** | {surface} | {reproduced_label} | `{rule}` | `{phase}` / `{event}` |".format(
                    **row
                )
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
    return _sha256(
        {
            "build_hash": build_hash,
            "build_path": str(config.build.resolve()),
            "project_root": str(config.project_root.resolve()),
            "api_endpoint": _public_api_endpoint_hash(config.api_url),
            "headless": config.headless,
            "quiet": config.quiet,
            "steering_model": STEERING_MODEL,
            "inspector_model": INSPECTOR_MODEL,
            "inspector_effort": INSPECTOR_EFFORT,
            "inspector_prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
            "rubric": _file_hash(
                config.project_root.resolve() / "config" / "qa-detection-rubric.json"
            ),
            "missions": [asdict(mission) for mission in MISSIONS],
            "seeds": SEEDS,
            "inspection_repetitions": INSPECTION_REPETITIONS,
            "logical_call_cap": LOGICAL_INSPECTION_CALL_CAP,
            "faults": [],
        }
    )


def _episode_hash(campaign_hash: str, spec: ExplorationEpisodeSpec) -> str:
    return _sha256({"campaign_hash": campaign_hash, "spec": asdict(spec)})


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
    if checkpoint.reusable(spec.unit_id, input_hash, [path, launch_path]):
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
            error=f"{type(error).__name__}: {error}",
        )
    _atomic_write_json(path, result.to_dict())
    if (
        result.execution_status == "completed"
        and result.coverage_status == "reached"
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
        except CampaignContractError:
            raise
        except Exception:
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
            "hashes": {
                "build": build_hash,
                "config": campaign_hash,
                "api_endpoint": _public_api_endpoint_hash(config.api_url),
            },
        },
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
    output = config.output.resolve()
    if output.exists() and any(output.iterdir()) and not config.resume:
        raise CampaignContractError("non-empty exploration output requires --resume")
    checkpoint = _prepare_campaign_output(output, campaign_hash)
    resumed = bool(checkpoint.units)
    _write_running_manifest(output, campaign_hash, build_hash, config)
    budget = InspectionCallBudget(
        cap=LOGICAL_INSPECTION_CALL_CAP,
        logical_calls=checkpoint.logical_calls,
        http_attempts=checkpoint.http_attempts,
    )
    records: list[ExplorationTraceRecord] = []
    trace_manifest: list[dict[str, Any]] = []
    try:
        for spec in build_track_a_schedule():
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
            passes = _inspect_record(
                opaque_trace_id=opaque_id,
                result=result,
                output=output,
                inspector=inspector,
                checkpoint=checkpoint,
                budget=budget,
                campaign_hash=campaign_hash,
            )
            record = ExplorationTraceRecord(
                spec=spec,
                opaque_trace_id=opaque_id,
                result=result,
                inspection_passes=tuple(passes),
                resumed=episode_resumed,
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
                    "resumed": episode_resumed,
                }
            )
        report = build_exploration_report(
            records,
            metadata={
                "campaign_id": campaign_hash[:16],
                "track": "A",
                "build_hash": build_hash,
                "steering_model": STEERING_MODEL,
                "inspection_model": INSPECTOR_MODEL,
            },
        )
        report_path = output / "exploration-report.json"
        markdown_path = output / "exploration-report.ko.md"
        report_path.write_text(exploration_report_json(report), encoding="utf-8")
        markdown_path.write_text(render_exploration_markdown(report), encoding="utf-8")
        manifest_path = output / "campaign-manifest.json"
        _atomic_write_json(
            manifest_path,
            {
                "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
                "campaign_hash": campaign_hash,
                "status": "complete",
                "track": "A",
                "hashes": {
                    "build": build_hash,
                    "config": campaign_hash,
                    "steering_model": _sha256(STEERING_MODEL),
                    "inspector_model": _sha256(INSPECTOR_MODEL),
                    "inspector_prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
                    "rubric": _file_hash(
                        config.project_root / "config" / "qa-detection-rubric.json"
                    ),
                    "api_endpoint": _public_api_endpoint_hash(config.api_url),
                },
                "models": {
                    "steering": STEERING_MODEL,
                    "inspection": INSPECTOR_MODEL,
                    "inspection_effort": INSPECTOR_EFFORT,
                },
                "limits": {
                    "inspection_repetitions": INSPECTION_REPETITIONS,
                    "planned_inspection_calls": PLANNED_INSPECTION_CALLS,
                    "logical_inspection_call_cap": LOGICAL_INSPECTION_CALL_CAP,
                },
                "counts": {
                    "scheduled_traces": len(records),
                    "eligible_gameplay_traces": report["summary"][
                        "eligible_gameplay_traces"
                    ],
                    "candidate_count": report["summary"]["candidate_count"],
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
        return ExplorationCampaignResult(
            manifest_path=manifest_path,
            report_path=report_path,
            records=tuple(records),
            resumed=resumed,
        )
    except Exception as error:
        _atomic_write_json(
            output / "campaign-manifest.json",
            {
                "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
                "campaign_hash": campaign_hash,
                "status": "incomplete",
                "track": "A",
                "hashes": {"build": build_hash, "config": campaign_hash},
                "counts": {
                    "logical_inspection_calls": checkpoint.logical_calls,
                    "http_attempts": checkpoint.http_attempts,
                },
                "failure": {"error_type": type(error).__name__},
            },
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


def _anomaly_artifacts(
    transitions: Sequence[dict[str, Any]],
    report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validations: list[dict[str, Any]] = []
    harness: list[dict[str, Any]] = []
    for anomaly in report.get("rule_based_anomalies") or []:
        if not isinstance(anomaly, dict):
            continue
        kind = _normalize_token(anomaly.get("kind"))
        if kind.startswith("llm_"):
            continue
        if kind == "bridge_action_failed":
            harness.append(
                {"kind": "bridge_failure", "detail": str(anomaly.get("evidence") or "")}
            )
            continue
        try:
            step = int(anomaly.get("step", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= step < len(transitions)):
            continue
        observation_value = transitions[step].get("observation") or {}
        observation_id = str(observation_value.get("observation_id") or "")
        if not observation_id:
            continue
        field_name = ""
        category = "behavior"
        rule = kind
        if kind == "view_state_match":
            category = "numeric"
            rule = "numeric-relation"
            field_name = "player_view.health_ratio"
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
            }
        )
    return validations, harness


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
            "--inspector-model",
            "",
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
        try:
            transitions = [
                json.loads(line)
                for line in (output_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if any(not isinstance(item, dict) for item in transitions):
                raise ValueError("steps.jsonl must contain JSON objects")
            report = _read_json_object(output_dir / "report.json")
            verdict = _read_json_object(output_dir / "verdict.json")
            run = _read_json_object(output_dir / "run.json")
        except (CampaignContractError, OSError, json.JSONDecodeError, ValueError) as error:
            return ExplorationEpisodeResult(
                transitions=[],
                execution_status="schema_error",
                coverage_status="error",
                error=f"invalid session artifact: {error}",
            )
        arguments = run.get("arguments") or {}
        launch_fault = str(arguments.get("fault") or "").strip()
        validations, harness = _anomaly_artifacts(transitions, report)
        return ExplorationEpisodeResult(
            transitions=transitions,
            execution_status=str(verdict.get("execution_status") or "infrastructure_error"),
            coverage_status=_mission_coverage_status(spec.mission_id, transitions),
            launch_faults=(launch_fault,) if launch_fault else (),
            invariant_validations=tuple(validations),
            harness_failures=tuple(harness),
            error=str(report.get("fatal_error") or ""),
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
    "run_exploration_campaign",
    "validate_track_a_spec",
]
