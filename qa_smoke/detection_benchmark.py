"""Private numeric scoring and reports for injected-fault inspection campaigns."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence

from .inspector import INSPECTION_SCHEMA_V1, INSPECTION_SCHEMA_V2, normalize_inspection_artifact


BENCHMARK_SCHEMA = "qa-detection-benchmark/v1"
FLOAT_TOLERANCE = 1e-5
FAULT_IDS = (
    "health_ratio_out_of_range",
    "relative_position_mismatch",
    "upgrade_ack_without_effect",
    "chest_collected_without_state_transition",
    "experience_level_drift",
    "currency_leak_across_restart",
    "hp_not_decreased_on_hit",
    "health_bar_desync",
    "item_effect_not_applied",
    "item_hit_range_mismatch",
    "experience_display_drift",
)
INVALID_STATUSES = (
    "BASELINE_CONFLICT",
    "FAULT_NOT_ACTIVATED",
    "NOT_REACHED",
    "UNOBSERVABLE",
    "INSPECTION_ERROR",
    "ERROR",
)

TraceVariant = Literal["clean", "fault"]
TraceStatus = Literal[
    "TP",
    "FN",
    "FP",
    "TN",
    "BASELINE_CONFLICT",
    "FAULT_NOT_ACTIVATED",
    "NOT_REACHED",
    "UNOBSERVABLE",
    "INSPECTION_ERROR",
    "ERROR",
]


@dataclass(frozen=True)
class TraceEvaluation:
    """All evaluator-private inputs required to score one inspected trace."""

    trace_id: str
    fault_id: str
    variant: TraceVariant
    execution_status: str
    coverage_status: str
    oracle_verdict: str
    transitions: Sequence[dict[str, Any]]
    inspection_passes: Sequence[dict[str, Any] | None]

    @classmethod
    def clean(
        cls,
        trace_id: str,
        fault_id: str,
        transitions: Sequence[dict[str, Any]],
        inspection_passes: Sequence[dict[str, Any] | None],
    ) -> TraceEvaluation:
        return cls(
            trace_id,
            fault_id,
            "clean",
            "completed",
            "reached",
            "pass",
            transitions,
            inspection_passes,
        )

    @classmethod
    def fault(
        cls,
        trace_id: str,
        fault_id: str,
        transitions: Sequence[dict[str, Any]],
        inspection_passes: Sequence[dict[str, Any] | None],
    ) -> TraceEvaluation:
        return cls(
            trace_id,
            fault_id,
            "fault",
            "completed",
            "reached",
            "fail",
            transitions,
            inspection_passes,
        )


@dataclass(frozen=True)
class NumericTarget:
    field: str
    comparison: str
    expected_value: int | float
    observed_value: int | float
    evidence_refs: tuple[str, ...]
    integer_values: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "comparison": self.comparison,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
            "evidence_refs": list(self.evidence_refs),
            "integer_values": self.integer_values,
        }


@dataclass
class TraceScore:
    trace_id: str
    fault_id: str
    variant: TraceVariant
    status: TraceStatus
    valid_passes: int = 0
    detected_passes: int = 0
    agreeing_passes: int = 0
    target_relations: list[dict[str, Any]] = field(default_factory=list)
    target_findings: list[dict[str, Any]] = field(default_factory=list)
    incidental_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PairScore:
    pair_id: str
    fault_id: str
    status: str
    clean: TraceScore
    fault: TraceScore

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "fault_id": self.fault_id,
            "status": self.status,
            "clean": self.clean.to_dict(),
            "fault": self.fault.to_dict(),
        }


def _observation(transition: Mapping[str, Any]) -> dict[str, Any]:
    value = transition.get("observation") or {}
    return value if isinstance(value, dict) else {}


def _decision(transition: Mapping[str, Any]) -> dict[str, Any]:
    value = transition.get("decision") or {}
    return value if isinstance(value, dict) else {}


def _reference(transition: Mapping[str, Any]) -> str:
    return str(_observation(transition).get("observation_id") or "")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _refs(*transitions: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for item in transitions if (ref := _reference(item))))


def _health_ratio_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        player = _observation(transition).get("player") or {}
        health = _number(player.get("health"))
        maximum = _number(player.get("max_health"))
        observed = _number(player.get("health_ratio"))
        refs = _refs(transition)
        if health is None or maximum is None or observed is None or maximum <= 0 or not refs:
            continue
        targets.append(
            NumericTarget("player.health_ratio", "!=", health / maximum, observed, refs)
        )
    return targets


def _relative_position_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        observation = _observation(transition)
        position = (observation.get("player") or {}).get("position") or {}
        player_x = _number(position.get("x"))
        player_y = _number(position.get("y"))
        entities = (observation.get("world") or {}).get("qa_entities") or []
        refs = _refs(transition)
        if player_x is None or player_y is None or not isinstance(entities, list) or not refs:
            continue
        public_entities = sorted(
            (item for item in entities if isinstance(item, dict)),
            key=lambda item: float(item.get("distance", float("inf")) or float("inf")),
        )[:8]
        for index, entity in enumerate(public_entities):
            x = _number(entity.get("x"))
            y = _number(entity.get("y"))
            relative_x = _number(entity.get("relative_x"))
            relative_y = _number(entity.get("relative_y"))
            if x is not None and relative_x is not None:
                targets.append(
                    NumericTarget(
                        f"world.threat_entities[{index}].relative_x",
                        "!=",
                        x - player_x,
                        relative_x,
                        refs,
                    )
                )
            if y is not None and relative_y is not None:
                targets.append(
                    NumericTarget(
                        f"world.threat_entities[{index}].relative_y",
                        "!=",
                        y - player_y,
                        relative_y,
                        refs,
                    )
                )
    return targets


def _owned_abilities(observation: Mapping[str, Any]) -> list[dict[str, Any]]:
    abilities = (observation.get("inventory") or {}).get("abilities") or []
    return [
        item
        for item in abilities
        if isinstance(item, dict) and item.get("owned") is not False
    ]


def _upgrade_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for index, transition in enumerate(transitions):
        if _decision(transition).get("action") != "select_upgrade" or index == 0:
            continue
        before_transition = transitions[index - 1]
        before = _owned_abilities(_observation(before_transition))
        after = _owned_abilities(_observation(transition))
        before_levels = {
            str(item.get("name") or item.get("type") or ""): _integer(item.get("level"))
            for item in before
        }
        refs = _refs(before_transition, transition)
        if not refs:
            continue
        for ability_index, item in enumerate(after):
            level = _integer(item.get("level"))
            name = str(item.get("name") or item.get("type") or "")
            previous = before_levels.get(name)
            if level is None or not name:
                continue
            expected = 1 if previous is None else previous + 1
            targets.append(
                NumericTarget(
                    f"inventory.abilities[{ability_index}].level",
                    "!=",
                    expected,
                    level,
                    refs,
                    integer_values=True,
                )
            )
    return targets


def _chest_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for index, transition in enumerate(transitions):
        event = _observation(transition).get("event_state") or {}
        if event.get("type") != "chest_collected" or index == 0:
            continue
        before_transition = transitions[index - 1]
        before = _integer((_observation(before_transition).get("world") or {}).get("chest_count"))
        after = _integer((_observation(transition).get("world") or {}).get("chest_count"))
        refs = _refs(before_transition, transition)
        if before is None or after is None or before <= 0 or not refs:
            continue
        targets.append(
            NumericTarget(
                "world.chest_count", "!=", before - 1, after, refs, integer_values=True
            )
        )
    return targets


def _experience_ratio_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        player = _observation(transition).get("player") or {}
        level = _integer(player.get("level"))
        experience = _number(player.get("exp"))
        required = _number(player.get("next_level_exp"))
        observed = _number(player.get("exp_ratio"))
        refs = _refs(transition)
        if (
            level is None
            or level < 3
            or experience is None
            or required is None
            or required <= 0
            or observed is None
            or not refs
        ):
            continue
        targets.append(
            NumericTarget("player.exp_ratio", "!=", experience / required, observed, refs)
        )
    return targets


def _restart_currency_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for index, transition in enumerate(transitions):
        if _decision(transition).get("action") != "restart":
            continue
        observed = _integer((_observation(transition).get("progress") or {}).get("coins_gained"))
        refs = _refs(*(transitions[max(0, index - 1) : index + 1]))
        if observed is not None and refs:
            targets.append(
                NumericTarget(
                    "progress.coins_gained", "!=", 0, observed, refs, integer_values=True
                )
            )
    return targets


def _health_hit_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for index, transition in enumerate(transitions):
        event = _observation(transition).get("event_state") or {}
        if event.get("type") not in {"player_hit", "hit"} or index == 0:
            continue
        before_transition = transitions[index - 1]
        before = _number((_observation(before_transition).get("player") or {}).get("health"))
        after = _number((_observation(transition).get("player") or {}).get("health"))
        refs = _refs(before_transition, transition)
        if before is not None and after is not None and refs:
            targets.append(NumericTarget("player.health", ">=", before, after, refs))
    return targets


def _view_health_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        observation = _observation(transition)
        raw = _number((observation.get("player") or {}).get("health"))
        displayed = _number((observation.get("player_view") or {}).get("health"))
        refs = _refs(transition)
        if raw is not None and displayed is not None and refs:
            targets.append(NumericTarget("player_view.health", "!=", raw, displayed, refs))
    return targets


def _item_effect_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for index, transition in enumerate(transitions):
        event = _observation(transition).get("event_state") or {}
        if event.get("type") != "item_used" or index == 0:
            continue
        before_transition = transitions[index - 1]
        before = _number((_observation(before_transition).get("progress") or {}).get("damage_dealt"))
        after = _number((_observation(transition).get("progress") or {}).get("damage_dealt"))
        refs = _refs(before_transition, transition)
        if before is not None and after is not None and refs:
            targets.append(NumericTarget("progress.damage_dealt", "<=", before, after, refs))
    return targets


def _item_range_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        event = _observation(transition).get("event_state") or {}
        if event.get("type") != "item_used":
            continue
        affected = _integer(event.get("targets_affected"))
        expected = _integer(event.get("targets_in_radius"))
        refs = _refs(transition)
        if affected is not None and expected is not None and refs:
            targets.append(
                NumericTarget(
                    "event_state.targets_affected",
                    "<",
                    expected,
                    affected,
                    refs,
                    integer_values=True,
                )
            )
    return targets


def _view_experience_targets(transitions: Sequence[dict[str, Any]]) -> list[NumericTarget]:
    targets: list[NumericTarget] = []
    for transition in transitions:
        observation = _observation(transition)
        level = _integer((observation.get("player") or {}).get("level"))
        raw = _number((observation.get("player") or {}).get("exp"))
        displayed = _number((observation.get("player_view") or {}).get("exp"))
        refs = _refs(transition)
        if level is not None and level >= 3 and raw is not None and displayed is not None and refs:
            targets.append(NumericTarget("player_view.exp", "!=", raw, displayed, refs))
    return targets


_TARGET_BUILDERS = {
    "health_ratio_out_of_range": _health_ratio_targets,
    "relative_position_mismatch": _relative_position_targets,
    "upgrade_ack_without_effect": _upgrade_targets,
    "chest_collected_without_state_transition": _chest_targets,
    "experience_level_drift": _experience_ratio_targets,
    "currency_leak_across_restart": _restart_currency_targets,
    "hp_not_decreased_on_hit": _health_hit_targets,
    "health_bar_desync": _view_health_targets,
    "item_effect_not_applied": _item_effect_targets,
    "item_hit_range_mismatch": _item_range_targets,
    "experience_display_drift": _view_experience_targets,
}


def private_numeric_targets(
    fault_id: str, transitions: Sequence[dict[str, Any]]
) -> list[NumericTarget]:
    """Build evaluator-private target relations from raw trace evidence."""

    builder = _TARGET_BUILDERS.get(fault_id)
    return builder(transitions) if builder is not None else []


def _artifact_from_pass(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("status") == "ERROR":
        return None
    candidate = value.get("artifact") if value.get("status") == "SUCCESS" else value
    if not isinstance(candidate, dict):
        return None
    if candidate.get("schema_version") not in (None, INSPECTION_SCHEMA_V1, INSPECTION_SCHEMA_V2):
        return None
    if not isinstance(candidate.get("findings"), list):
        return None
    return candidate


def _numeric_equal(value: Any, target: int | float, integer_values: bool) -> bool:
    numeric = _number(value)
    if numeric is None:
        return False
    if integer_values:
        return float(numeric).is_integer() and int(numeric) == target
    return math.isclose(float(numeric), float(target), rel_tol=0.0, abs_tol=FLOAT_TOLERANCE)


def _matches_target(finding: Mapping[str, Any], target: NumericTarget) -> bool:
    if finding.get("kind") != "numeric":
        return False
    if finding.get("field") != target.field or finding.get("comparison") != target.comparison:
        return False
    if not _numeric_equal(finding.get("expected_value"), target.expected_value, target.integer_values):
        return False
    if not _numeric_equal(finding.get("observed_value"), target.observed_value, target.integer_values):
        return False
    finding_refs = {str(ref) for ref in finding.get("evidence_refs") or []}
    return bool(target.evidence_refs) and set(target.evidence_refs).issubset(finding_refs)


def _initial_status(trace: TraceEvaluation) -> TraceStatus | None:
    if trace.execution_status != "completed":
        return "ERROR"
    if trace.coverage_status != "reached" or trace.oracle_verdict == "not_evaluated":
        return "NOT_REACHED"
    if trace.variant == "clean" and trace.oracle_verdict != "pass":
        return "BASELINE_CONFLICT"
    if trace.variant == "fault" and trace.oracle_verdict != "fail":
        return "FAULT_NOT_ACTIVATED"
    return None


def score_trace(trace: TraceEvaluation) -> TraceScore:
    """Collapse three inspection passes into one trace-level confusion verdict."""

    if trace.variant not in {"clean", "fault"}:
        raise ValueError(f"unsupported trace variant: {trace.variant}")
    if trace.fault_id not in _TARGET_BUILDERS:
        return TraceScore(trace.trace_id, trace.fault_id, trace.variant, "UNOBSERVABLE")
    initial_status = _initial_status(trace)
    if initial_status is not None:
        return TraceScore(trace.trace_id, trace.fault_id, trace.variant, initial_status)
    targets = private_numeric_targets(trace.fault_id, trace.transitions)
    if not targets:
        return TraceScore(trace.trace_id, trace.fault_id, trace.variant, "UNOBSERVABLE")
    if len(trace.inspection_passes) > 3:
        raise ValueError("a trace accepts exactly three inspection passes")

    target_findings: list[dict[str, Any]] = []
    incidental: list[dict[str, Any]] = []
    pass_votes: list[bool] = []
    passes = [*trace.inspection_passes, *([None] * (3 - len(trace.inspection_passes)))]
    for pass_index, pass_value in enumerate(passes, start=1):
        artifact = _artifact_from_pass(pass_value)
        if artifact is None:
            continue
        normalized = normalize_inspection_artifact(artifact)
        matches: list[dict[str, Any]] = []
        for finding in normalized["findings"]:
            recorded = {**finding, "inspection_pass": pass_index}
            if any(_matches_target(finding, target) for target in targets):
                matches.append(recorded)
            else:
                incidental.append(recorded)
        target_findings.extend(matches)
        pass_votes.append(bool(matches))

    detected_passes = sum(pass_votes)
    missed_passes = len(pass_votes) - detected_passes
    if detected_passes >= 2:
        status: TraceStatus = "TP" if trace.variant == "fault" else "FP"
        agreeing = detected_passes
    elif missed_passes >= 2:
        status = "FN" if trace.variant == "fault" else "TN"
        agreeing = missed_passes
    else:
        status = "INSPECTION_ERROR"
        agreeing = max(detected_passes, missed_passes)
    return TraceScore(
        trace_id=trace.trace_id,
        fault_id=trace.fault_id,
        variant=trace.variant,
        status=status,
        valid_passes=len(pass_votes),
        detected_passes=detected_passes,
        agreeing_passes=agreeing,
        target_relations=[target.to_dict() for target in targets],
        target_findings=target_findings,
        incidental_candidates=incidental,
    )


def score_pair(pair_id: str, clean: TraceEvaluation, fault: TraceEvaluation) -> PairScore:
    """Score a symmetric clean/fault pair and expose an explicit validity status."""

    if clean.variant != "clean" or fault.variant != "fault":
        raise ValueError("score_pair requires clean then fault variants")
    if clean.fault_id != fault.fault_id:
        raise ValueError("paired traces must target the same fault")
    clean_score = score_trace(clean)
    fault_score = score_trace(fault)
    invalid = [
        status
        for status in INVALID_STATUSES
        if clean_score.status == status or fault_score.status == status
    ]
    return PairScore(
        pair_id=pair_id,
        fault_id=clean.fault_id,
        status=invalid[0] if invalid else "VALID",
        clean=clean_score,
        fault=fault_score,
    )


def wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    """Return the two-sided Wilson score interval with z=1.96."""

    if total <= 0:
        return None, None
    if successes < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    z = 1.959963984540054
    proportion_value = successes / total
    denominator = 1.0 + (z * z / total)
    center = (proportion_value + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion_value * (1.0 - proportion_value) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def proportion(numerator: int, denominator: int) -> dict[str, Any]:
    low, high = wilson_interval(numerator, denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "wilson_95": {"low": low, "high": high},
    }


def _macro_detection(per_fault: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    rates = [
        item["detection_rate"]
        for item in per_fault.values()
        if item["detection_rate"]["denominator"]
    ]
    if not rates:
        return {
            "faults_included": 0,
            "value": None,
            "wilson_95": {"low": None, "high": None},
        }
    return {
        "faults_included": len(rates),
        "value": sum(item["value"] for item in rates) / len(rates),
        "wilson_95": {
            "low": sum(item["wilson_95"]["low"] for item in rates) / len(rates),
            "high": sum(item["wilson_95"]["high"] for item in rates) / len(rates),
        },
    }


def aggregate_benchmark(pairs: Sequence[PairScore]) -> dict[str, Any]:
    """Aggregate only valid pairs into trace-level conditional detection metrics."""

    counts = {
        "pairs_total": len(pairs),
        "pairs_valid": 0,
        "TP": 0,
        "FN": 0,
        "FP": 0,
        "TN": 0,
        **{status: 0 for status in INVALID_STATUSES},
    }
    by_fault: dict[str, list[PairScore]] = defaultdict(list)
    for pair in pairs:
        if pair.status != "VALID":
            counts[pair.status] += 1
            continue
        counts["pairs_valid"] += 1
        counts[pair.clean.status] += 1
        counts[pair.fault.status] += 1
        by_fault[pair.fault_id].append(pair)

    per_fault: dict[str, dict[str, Any]] = {}
    for fault_id in FAULT_IDS:
        fault_pairs = by_fault.get(fault_id, [])
        tp = sum(pair.fault.status == "TP" for pair in fault_pairs)
        fn = sum(pair.fault.status == "FN" for pair in fault_pairs)
        fp = sum(pair.clean.status == "FP" for pair in fault_pairs)
        tn = sum(pair.clean.status == "TN" for pair in fault_pairs)
        paired_success = sum(
            pair.clean.status == "TN" and pair.fault.status == "TP" for pair in fault_pairs
        )
        per_fault[fault_id] = {
            "counts": {"pairs": len(fault_pairs), "TP": tp, "FN": fn, "FP": fp, "TN": tn},
            "detection_rate": proportion(tp, tp + fn),
            "clean_false_positive_rate": proportion(fp, fp + tn),
            "paired_success_rate": proportion(paired_success, len(fault_pairs)),
        }

    valid_pairs = [pair for pair in pairs if pair.status == "VALID"]
    paired_successes = sum(
        pair.clean.status == "TN" and pair.fault.status == "TP" for pair in valid_pairs
    )
    valid_traces = [trace for pair in valid_pairs for trace in (pair.clean, pair.fault)]
    agreement_numerator = sum(trace.agreeing_passes for trace in valid_traces)
    agreement_denominator = sum(trace.valid_passes for trace in valid_traces)
    metrics = {
        "macro_detection_rate": _macro_detection(per_fault),
        "micro_detection_rate": proportion(counts["TP"], counts["TP"] + counts["FN"]),
        "coverage": proportion(counts["pairs_valid"], counts["pairs_total"]),
        "precision": proportion(counts["TP"], counts["TP"] + counts["FP"]),
        "specificity": proportion(counts["TN"], counts["TN"] + counts["FP"]),
        "clean_false_positive_rate": proportion(counts["FP"], counts["FP"] + counts["TN"]),
        "paired_success_rate": proportion(paired_successes, counts["pairs_valid"]),
        "inspection_agreement": proportion(agreement_numerator, agreement_denominator),
    }
    return {"counts": counts, "metrics": metrics, "per_fault": per_fault}


def build_benchmark_report(
    pairs: Sequence[PairScore], *, metadata: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    aggregate = aggregate_benchmark(pairs)
    return {
        "schema_version": BENCHMARK_SCHEMA,
        "metadata": dict(metadata or {}),
        **aggregate,
        "pairs": [pair.to_dict() for pair in pairs],
    }


def benchmark_report_json(report: Mapping[str, Any]) -> str:
    if report.get("schema_version") != BENCHMARK_SCHEMA:
        raise ValueError(f"report schema must be {BENCHMARK_SCHEMA}")
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _percentage(rate: Mapping[str, Any]) -> str:
    value = rate.get("value")
    return "N/A" if value is None else f"{float(value):.2%}"


def _raw_rate(rate: Mapping[str, Any]) -> str:
    if "numerator" in rate:
        return f"{rate['numerator']}/{rate['denominator']}"
    return f"{rate.get('faults_included', 0)} faults"


def _interval(rate: Mapping[str, Any]) -> str:
    interval = rate.get("wilson_95") or {}
    low = interval.get("low")
    high = interval.get("high")
    if low is None or high is None:
        return "N/A"
    return f"{float(low):.2%}–{float(high):.2%}"


def render_benchmark_markdown(report: Mapping[str, Any]) -> str:
    """Render a Korean operator report while preserving standard English metric names."""

    if report.get("schema_version") != BENCHMARK_SCHEMA:
        raise ValueError(f"report schema must be {BENCHMARK_SCHEMA}")
    counts = report["counts"]
    metrics = report["metrics"]
    metadata = report.get("metadata") or {}
    lines = [
        "# 주입 결함 탐지 벤치마크",
        "",
        f"- 스키마: `{BENCHMARK_SCHEMA}`",
    ]
    if metadata.get("campaign_id"):
        lines.append(f"- 캠페인: `{metadata['campaign_id']}`")
    lines.extend(
        [
            "",
            "## 추적 단위 혼동 행렬",
            "",
            "세 번의 inspection pass를 하나의 trace verdict로 합산했습니다.",
            "",
            "| 구분 | 개수 |",
            "| --- | ---: |",
            f"| TP | {counts['TP']} |",
            f"| FN | {counts['FN']} |",
            f"| FP | {counts['FP']} |",
            f"| TN | {counts['TN']} |",
            "",
            "## 지표",
            "",
            "| Metric | 값 | Raw | Wilson 95% CI |",
            "| --- | ---: | ---: | ---: |",
            *(
                f"| {label} | {_percentage(metrics[key])} | {_raw_rate(metrics[key])} | "
                f"{_interval(metrics[key])} |"
                for label, key in (
                    ("Macro detection rate", "macro_detection_rate"),
                    ("Micro detection rate", "micro_detection_rate"),
                    ("Coverage", "coverage"),
                    ("Precision", "precision"),
                    ("Specificity", "specificity"),
                    ("Clean false-positive rate", "clean_false_positive_rate"),
                    ("Paired success rate", "paired_success_rate"),
                    ("Inspection agreement", "inspection_agreement"),
                )
            ),
            "",
            "## Fault별 결과",
            "",
            "| Fault | TP | FN | FP | TN | Detection rate | Raw | Wilson 95% CI |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            *(
                f"| {fault_id} | {item['counts']['TP']} | {item['counts']['FN']} | "
                f"{item['counts']['FP']} | {item['counts']['TN']} | "
                f"{_percentage(item['detection_rate'])} | {_raw_rate(item['detection_rate'])} | "
                f"{_interval(item['detection_rate'])} |"
                for fault_id, item in report["per_fault"].items()
            ),
            "",
            "## 유효하지 않은 쌍",
            "",
            "이 상태들은 conditional detection denominator에 포함되지 않습니다.",
            "",
            "| 상태 | 개수 |",
            "| --- | ---: |",
        ]
    )
    lines.extend(f"| {status} | {counts[status]} |" for status in INVALID_STATUSES)
    return "\n".join(lines) + "\n"
