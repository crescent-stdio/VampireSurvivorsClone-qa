from __future__ import annotations

import json

import pytest

from . import detection_benchmark as benchmark


def numeric_finding(
    field: str,
    comparison: str,
    expected: float,
    observed: float,
    evidence_refs: list[str],
) -> dict[str, object]:
    return {
        "kind": "numeric",
        "field": field,
        "comparison": comparison,
        "expected_value": expected,
        "observed_value": observed,
        "statement": "The target numeric relation is inconsistent.",
        "evidence_refs": evidence_refs,
    }


def artifact(*findings: dict[str, object]) -> dict[str, object]:
    return {"schema_version": "qa-inspection/v2", "findings": list(findings)}


FAULT_CASES = (
    (
        "health_ratio_out_of_range",
        [{"observation": {"observation_id": "obs-h", "player": {"present": True, "health": 96.0, "max_health": 100.0, "health_ratio": 1.25}}}],
        numeric_finding("player.health_ratio", "!=", 0.96, 1.25, ["obs-h"]),
    ),
    (
        "relative_position_mismatch",
        [{"observation": {"observation_id": "obs-r", "player": {"present": True, "position": {"x": 10.0, "y": 3.0}}, "world": {"qa_entities": [{"x": 15.0, "y": 5.0, "relative_x": 12.0, "relative_y": 2.0}]}}}],
        numeric_finding("world.threat_entities[0].relative_x", "!=", 5.0, 12.0, ["obs-r"]),
    ),
    (
        "upgrade_ack_without_effect",
        [
            {"observation": {"observation_id": "obs-u0", "menu": {"choices": [{"name": "Fire", "level": 1, "owned": True}, {"name": "Ice", "level": 2, "owned": True}]}, "inventory": {"abilities": [{"name": "Fire", "level": 1, "owned": True}, {"name": "Ice", "level": 2, "owned": True}]}}},
            {"decision": {"action": "select_upgrade", "arguments": {"index": 0}}, "observation": {"observation_id": "obs-u1", "inventory": {"abilities": [{"name": "Fire", "level": 1, "owned": True}, {"name": "Ice", "level": 2, "owned": True}]}}},
        ],
        numeric_finding("inventory.abilities[0].level", "!=", 2, 1, ["obs-u0", "obs-u1"]),
    ),
    (
        "chest_collected_without_state_transition",
        [
            {"observation": {"observation_id": "obs-c0", "world": {"chest_count": 2}}},
            {"observation": {"observation_id": "obs-c1", "world": {"chest_count": 2}, "event_state": {"type": "chest_collected", "event_id": "event-c"}}},
        ],
        numeric_finding("world.chest_count", "!=", 1, 2, ["obs-c0", "obs-c1"]),
    ),
    (
        "experience_level_drift",
        [{"observation": {"observation_id": "obs-x", "player": {"present": True, "level": 3, "exp": 13.0, "next_level_exp": 20.0, "exp_ratio": 0.5}}}],
        numeric_finding("player.exp_ratio", "!=", 0.65, 0.5, ["obs-x"]),
    ),
    (
        "currency_leak_across_restart",
        [
            {"observation": {"observation_id": "obs-k0", "progress": {"coins_gained": 7}}},
            {"decision": {"action": "restart"}, "observation": {"observation_id": "obs-k1", "progress": {"coins_gained": 7}}},
        ],
        numeric_finding("progress.coins_gained", "!=", 0, 7, ["obs-k0", "obs-k1"]),
    ),
    (
        "hp_not_decreased_on_hit",
        [
            {"observation": {"observation_id": "obs-p0", "player": {"present": True, "health": 10.0}}},
            {"observation": {"observation_id": "obs-p1", "player": {"present": True, "health": 10.0}, "event_state": {"type": "player_hit"}}},
        ],
        numeric_finding("player.health", ">=", 10.0, 10.0, ["obs-p0", "obs-p1"]),
    ),
    (
        "health_bar_desync",
        [{"observation": {"observation_id": "obs-v", "player": {"present": True, "health": 8.0, "max_health": 10.0}, "player_view": {"health": 10.0, "health_ratio": 1.0}}}],
        numeric_finding("player_view.health", "!=", 8.0, 10.0, ["obs-v"]),
    ),
    (
        "item_effect_not_applied",
        [
            {"observation": {"observation_id": "obs-i0", "progress": {"damage_dealt": 40.0}}},
            {"observation": {"observation_id": "obs-i1", "progress": {"damage_dealt": 40.0}, "event_state": {"type": "item_used"}}},
        ],
        numeric_finding("progress.damage_dealt", "<=", 40.0, 40.0, ["obs-i0", "obs-i1"]),
    ),
    (
        "item_hit_range_mismatch",
        [{"observation": {"observation_id": "obs-a", "event_state": {"type": "item_used", "targets_affected": 1, "targets_in_radius": 3}}}],
        numeric_finding("event_state.targets_affected", "<", 3, 1, ["obs-a"]),
    ),
    (
        "experience_display_drift",
        [{"observation": {"observation_id": "obs-d", "player": {"present": True, "level": 3, "exp": 10.0}, "player_view": {"exp": 13.0}}}],
        numeric_finding("player_view.exp", "!=", 10.0, 13.0, ["obs-d"]),
    ),
)


EXPECTED_TARGET_RELATIONS = {
    "health_ratio_out_of_range": [
        {"field": "player.health_ratio", "comparison": "!=", "expected_value": 0.96, "observed_value": 1.25, "evidence_refs": ["obs-h"], "integer_values": False},
    ],
    "relative_position_mismatch": [
        {"field": "world.threat_entities[0].relative_x", "comparison": "!=", "expected_value": 5.0, "observed_value": 12.0, "evidence_refs": ["obs-r"], "integer_values": False},
    ],
    "upgrade_ack_without_effect": [
        {"field": "inventory.abilities[0].level", "comparison": "!=", "expected_value": 2, "observed_value": 1, "evidence_refs": ["obs-u0", "obs-u1"], "integer_values": True},
    ],
    "chest_collected_without_state_transition": [
        {"field": "world.chest_count", "comparison": "!=", "expected_value": 1, "observed_value": 2, "evidence_refs": ["obs-c0", "obs-c1"], "integer_values": True},
    ],
    "experience_level_drift": [
        {"field": "player.exp_ratio", "comparison": "!=", "expected_value": 0.65, "observed_value": 0.5, "evidence_refs": ["obs-x"], "integer_values": False},
    ],
    "currency_leak_across_restart": [
        {"field": "progress.coins_gained", "comparison": "!=", "expected_value": 0, "observed_value": 7, "evidence_refs": ["obs-k0", "obs-k1"], "integer_values": True},
    ],
    "hp_not_decreased_on_hit": [
        {"field": "player.health", "comparison": ">=", "expected_value": 10.0, "observed_value": 10.0, "evidence_refs": ["obs-p0", "obs-p1"], "integer_values": False},
    ],
    "health_bar_desync": [
        {"field": "player_view.health", "comparison": "!=", "expected_value": 8.0, "observed_value": 10.0, "evidence_refs": ["obs-v"], "integer_values": False},
        {"field": "player_view.health_ratio", "comparison": "!=", "expected_value": 0.8, "observed_value": 1.0, "evidence_refs": ["obs-v"], "integer_values": False},
    ],
    "item_effect_not_applied": [
        {"field": "progress.damage_dealt", "comparison": "<=", "expected_value": 40.0, "observed_value": 40.0, "evidence_refs": ["obs-i0", "obs-i1"], "integer_values": False},
    ],
    "item_hit_range_mismatch": [
        {"field": "event_state.targets_affected", "comparison": "<", "expected_value": 3, "observed_value": 1, "evidence_refs": ["obs-a"], "integer_values": True},
    ],
    "experience_display_drift": [
        {"field": "player_view.exp", "comparison": "!=", "expected_value": 10.0, "observed_value": 13.0, "evidence_refs": ["obs-d"], "integer_values": False},
    ],
}


def test_literal_fault_table_covers_the_complete_private_registry() -> None:
    assert tuple(case[0] for case in FAULT_CASES) == benchmark.FAULT_IDS


@pytest.mark.parametrize(("fault_id", "transitions", "finding"), FAULT_CASES)
def test_every_private_fault_requires_its_literal_numeric_relation(
    fault_id: str,
    transitions: list[dict[str, object]],
    finding: dict[str, object],
) -> None:
    trace = benchmark.TraceEvaluation(
        trace_id=f"fault-{fault_id}",
        fault_id=fault_id,
        variant="fault",
        execution_status="completed",
        coverage_status="reached",
        oracle_verdict="fail",
        transitions=transitions,
        inspection_passes=[artifact(finding), artifact(finding), artifact()],
    )

    result = benchmark.score_trace(trace)

    assert result.status == "TP"
    assert result.detected_passes == 2
    assert result.valid_passes == 3
    assert result.agreeing_passes == 2
    assert result.target_relations == EXPECTED_TARGET_RELATIONS[fault_id]
    assert [item["field"] for item in result.target_findings] == [
        str(finding["field"]),
        str(finding["field"]),
    ]


def test_relative_y_and_unselected_upgrade_ability_are_not_private_targets() -> None:
    relative_fault, relative_transitions, _ = FAULT_CASES[1]
    relative_y = numeric_finding(
        "world.threat_entities[0].relative_y", "!=", 2.0, 2.0, ["obs-r"]
    )
    upgrade_fault, upgrade_transitions, _ = FAULT_CASES[2]
    unselected = numeric_finding(
        "inventory.abilities[1].level", "!=", 3, 2, ["obs-u0", "obs-u1"]
    )

    relative = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "relative-y", relative_fault, relative_transitions, [artifact(relative_y)] * 3
        )
    )
    upgrade = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "unselected", upgrade_fault, upgrade_transitions, [artifact(unselected)] * 3
        )
    )

    assert relative.status == "FN"
    assert upgrade.status == "FN"


def test_private_targets_reject_non_injected_relative_and_upgrade_values() -> None:
    relative_transitions = [
        {
            "observation": {
                "observation_id": "obs-r-other",
                "player": {"present": True, "position": {"x": 10.0}},
                "world": {
                    "qa_entities": [{"x": 15.0, "relative_x": 8.0, "distance": 1.0}]
                },
            }
        }
    ]
    relative_finding = numeric_finding(
        "world.threat_entities[0].relative_x", "!=", 5.0, 8.0, ["obs-r-other"]
    )
    upgrade_transitions = [
        {
            "observation": {
                "observation_id": "obs-u-other-0",
                "menu": {"choices": [{"name": "Fire", "level": 1, "owned": True}]},
                "inventory": {
                    "abilities": [{"name": "Fire", "level": 1, "owned": True}]
                },
            }
        },
        {
            "decision": {"action": "select_upgrade", "arguments": {"index": 0}},
            "observation": {
                "observation_id": "obs-u-other-1",
                "inventory": {
                    "abilities": [{"name": "Fire", "level": 3, "owned": True}]
                },
            },
        },
    ]
    upgrade_finding = numeric_finding(
        "inventory.abilities[0].level",
        "!=",
        2,
        3,
        ["obs-u-other-0", "obs-u-other-1"],
    )

    relative = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "relative-other",
            "relative_position_mismatch",
            relative_transitions,
            [artifact(relative_finding)] * 3,
        )
    )
    upgrade = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "upgrade-other",
            "upgrade_ack_without_effect",
            upgrade_transitions,
            [artifact(upgrade_finding)] * 3,
        )
    )

    assert relative.status == "UNOBSERVABLE"
    assert upgrade.status == "UNOBSERVABLE"


def test_wrong_field_values_evidence_and_text_only_alerts_are_incidental() -> None:
    transitions = FAULT_CASES[0][1]
    wrong_field = numeric_finding("player.exp_ratio", "!=", 0.96, 1.25, ["obs-h"])
    wrong_comparison = numeric_finding("player.health_ratio", ">", 0.96, 1.25, ["obs-h"])
    wrong_values = numeric_finding("player.health_ratio", "!=", 0.95, 1.25, ["obs-h"])
    wrong_evidence = numeric_finding("player.health_ratio", "!=", 0.96, 1.25, ["obs-other"])
    text_only = {
        "kind": "behavior",
        "rule": "health ratios stay consistent",
        "expected_value": "consistent",
        "observed_value": "mismatch",
        "statement": "The health ratio looks wrong.",
        "evidence_refs": ["obs-h"],
    }
    malformed = {
        **numeric_finding("player.health_ratio", "!=", 0.96, 1.25, ["obs-h"]),
        "observed_value": "1.25",
    }
    trace = benchmark.TraceEvaluation(
        trace_id="fault-health",
        fault_id="health_ratio_out_of_range",
        variant="fault",
        execution_status="completed",
        coverage_status="reached",
        oracle_verdict="fail",
        transitions=transitions,
        inspection_passes=[
            artifact(
                wrong_field,
                wrong_comparison,
                wrong_values,
                wrong_evidence,
                text_only,
                malformed,
            ),
            artifact(),
            artifact(),
        ],
    )

    result = benchmark.score_trace(trace)

    assert result.status == "FN"
    assert result.target_findings == []
    assert len(result.incidental_candidates) == 5
    assert {item["kind"] for item in result.incidental_candidates} == {"numeric", "behavior"}


def test_target_finding_rejects_fabricated_extra_evidence_but_allows_real_extra_refs() -> None:
    fault_id, original_transitions, _ = FAULT_CASES[0]
    transitions = [
        *original_transitions,
        {
            "observation": {
                "observation_id": "obs-extra",
                "event_state": {"event_id": "event-extra"},
            }
        },
    ]
    fabricated = numeric_finding(
        "player.health_ratio", "!=", 0.96, 1.25, ["obs-h", "fabricated-ref"]
    )
    real_extra = numeric_finding(
        "player.health_ratio",
        "!=",
        0.96,
        1.25,
        ["obs-h", "obs-extra", "event-extra"],
    )

    rejected = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "fabricated", fault_id, transitions, [artifact(fabricated)] * 3
        )
    )
    accepted = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "real-extra", fault_id, transitions, [artifact(real_extra)] * 3
        )
    )

    assert rejected.status == "FN"
    assert len(rejected.incidental_candidates) == 3
    assert accepted.status == "TP"


def test_float_values_use_1e_5_tolerance_and_integer_values_are_exact() -> None:
    float_case = FAULT_CASES[0]
    near = numeric_finding("player.health_ratio", "!=", 0.960009, 1.249991, ["obs-h"])
    far = numeric_finding("player.health_ratio", "!=", 0.960011, 1.25, ["obs-h"])
    integer_case = FAULT_CASES[9]
    non_integer = numeric_finding("event_state.targets_affected", "<", 3, 1.000001, ["obs-a"])

    assert benchmark.score_trace(benchmark.TraceEvaluation.fault("near", float_case[0], float_case[1], [artifact(near)] * 3)).status == "TP"
    assert benchmark.score_trace(benchmark.TraceEvaluation.fault("far", float_case[0], float_case[1], [artifact(far)] * 3)).status == "FN"
    assert benchmark.score_trace(benchmark.TraceEvaluation.fault("integer", integer_case[0], integer_case[1], [artifact(non_integer)] * 3)).status == "FN"


def test_clean_false_positive_requires_the_private_target_relation() -> None:
    transitions = [{"observation": {"observation_id": "obs-clean", "player": {"present": True, "health": 50.0, "max_health": 100.0, "health_ratio": 0.5}}}]
    target_alert = numeric_finding("player.health_ratio", "!=", 0.5, 0.5, ["obs-clean"])
    unrelated_alert = numeric_finding("player.exp_ratio", "!=", 0.5, 0.7, ["obs-clean"])

    fp = benchmark.score_trace(benchmark.TraceEvaluation.clean("clean-fp", "health_ratio_out_of_range", transitions, [artifact(target_alert)] * 3))
    tn = benchmark.score_trace(benchmark.TraceEvaluation.clean("clean-tn", "health_ratio_out_of_range", transitions, [artifact(unrelated_alert)] * 3))

    assert fp.status == "FP"
    assert tn.status == "TN"
    assert len(tn.incidental_candidates) == 3


def test_fault_trace_scores_only_violated_post_activation_relations() -> None:
    transitions = [
        {
            "observation": {
                "observation_id": "obs-view-before",
                "player": {"present": True, "health": 10.0, "max_health": 10.0},
                "player_view": {"health": 10.0, "health_ratio": 1.0},
            }
        },
        {
            "observation": {
                "observation_id": "obs-view-after",
                "player": {"present": True, "health": 8.0, "max_health": 10.0},
                "player_view": {"health": 10.0, "health_ratio": 1.0},
            }
        },
    ]
    pre_activation_false_alert = numeric_finding(
        "player_view.health", "!=", 10.0, 10.0, ["obs-view-before"]
    )
    post_activation_violation = numeric_finding(
        "player_view.health", "!=", 8.0, 10.0, ["obs-view-after"]
    )

    pre_only = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "pre-only",
            "health_bar_desync",
            transitions,
            [artifact(pre_activation_false_alert), artifact(pre_activation_false_alert), artifact()],
        )
    )
    post = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "post",
            "health_bar_desync",
            transitions,
            [artifact(post_activation_violation), artifact(post_activation_violation), artifact()],
        )
    )

    assert pre_only.status == "FN"
    assert len(pre_only.incidental_candidates) == 2
    assert {relation["evidence_refs"][0] for relation in pre_only.target_relations} == {
        "obs-view-after"
    }
    assert post.status == "TP"


@pytest.mark.parametrize(
    ("variant", "execution", "coverage", "oracle", "transitions", "passes", "expected"),
    (
        ("clean", "completed", "reached", "fail", FAULT_CASES[0][1], [artifact()] * 3, "BASELINE_CONFLICT"),
        ("fault", "completed", "reached", "pass", FAULT_CASES[0][1], [artifact()] * 3, "FAULT_NOT_ACTIVATED"),
        ("fault", "completed", "not_reached", "not_evaluated", FAULT_CASES[0][1], [artifact()] * 3, "NOT_REACHED"),
        ("fault", "infrastructure_error", "reached", "not_evaluated", FAULT_CASES[0][1], [artifact()] * 3, "ERROR"),
        ("fault", "completed", "reached", "fail", [], [artifact()] * 3, "UNOBSERVABLE"),
        ("fault", "completed", "reached", "fail", FAULT_CASES[0][1], [None, None, artifact()], "INSPECTION_ERROR"),
    ),
)
def test_trace_statuses_remain_separate(
    variant: str,
    execution: str,
    coverage: str,
    oracle: str,
    transitions: list[dict[str, object]],
    passes: list[dict[str, object] | None],
    expected: str,
) -> None:
    result = benchmark.score_trace(
        benchmark.TraceEvaluation(
            trace_id="trace-status",
            fault_id="health_ratio_out_of_range",
            variant=variant,
            execution_status=execution,
            coverage_status=coverage,
            oracle_verdict=oracle,
            transitions=transitions,
            inspection_passes=passes,
        )
    )

    assert result.status == expected


def test_invalid_trace_still_preserves_normalized_incidental_candidates() -> None:
    fault_id, transitions, _ = FAULT_CASES[0]
    off_target = numeric_finding("player.exp_ratio", "!=", 0.1, 0.2, ["obs-h"])
    trace = benchmark.TraceEvaluation(
        "invalid-audit",
        fault_id,
        "fault",
        "contract_error",
        "reached",
        "not_evaluated",
        transitions,
        [artifact(off_target)] * 3,
    )

    result = benchmark.score_trace(trace)

    assert result.status == "ERROR"
    assert result.valid_passes == 3
    assert len(result.incidental_candidates) == 3
    assert {item["field"] for item in result.incidental_candidates} == {"player.exp_ratio"}


def test_two_matching_valid_passes_survive_one_inspection_error_but_split_votes_do_not() -> None:
    fault_id, transitions, finding = FAULT_CASES[0]
    decided = benchmark.TraceEvaluation.fault(
        "decided", fault_id, transitions, [artifact(finding), None, artifact(finding)]
    )
    split = benchmark.TraceEvaluation.fault(
        "split", fault_id, transitions, [artifact(finding), None, artifact()]
    )

    decided_result = benchmark.score_trace(decided)
    split_result = benchmark.score_trace(split)

    assert (decided_result.status, decided_result.valid_passes, decided_result.agreeing_passes) == ("TP", 2, 2)
    assert (split_result.status, split_result.valid_passes, split_result.agreeing_passes) == ("INSPECTION_ERROR", 2, 1)


def test_invalid_pairs_are_counted_by_status_but_excluded_from_confusion_denominators() -> None:
    fault_id, transitions, finding = FAULT_CASES[0]
    clean = benchmark.TraceEvaluation.clean("clean", fault_id, transitions, [artifact()] * 3)
    valid = benchmark.score_pair(
        "pair-valid",
        benchmark.TraceEvaluation.clean("clean-valid", fault_id, transitions, [artifact()] * 3),
        benchmark.TraceEvaluation.fault("fault-valid", fault_id, transitions, [artifact(finding)] * 3),
    )
    conflict = benchmark.score_pair(
        "pair-conflict",
        benchmark.TraceEvaluation("clean-conflict", fault_id, "clean", "completed", "reached", "fail", transitions, [artifact()] * 3),
        benchmark.TraceEvaluation("fault-conflict", fault_id, "fault", "contract_error", "reached", "not_evaluated", transitions, [artifact()] * 3),
    )
    not_activated = benchmark.score_pair(
        "pair-not-activated",
        clean,
        benchmark.TraceEvaluation("fault-pass", fault_id, "fault", "completed", "reached", "pass", transitions, [artifact()] * 3),
    )
    not_reached = benchmark.score_pair(
        "pair-not-reached",
        clean,
        benchmark.TraceEvaluation("fault-not-reached", fault_id, "fault", "completed", "not_reached", "not_evaluated", transitions, [artifact()] * 3),
    )
    unobservable = benchmark.score_pair(
        "pair-unobservable",
        clean,
        benchmark.TraceEvaluation.fault("fault-unobservable", fault_id, [], [artifact()] * 3),
    )
    inspection_error = benchmark.score_pair(
        "pair-inspection-error",
        clean,
        benchmark.TraceEvaluation.fault("fault-inspection-error", fault_id, transitions, [artifact(finding), artifact(), None]),
    )
    execution_error = benchmark.score_pair(
        "pair-error",
        clean,
        benchmark.TraceEvaluation("fault-error", fault_id, "fault", "contract_error", "reached", "not_evaluated", transitions, [artifact()] * 3),
    )

    invalid = [
        conflict,
        not_activated,
        not_reached,
        unobservable,
        inspection_error,
        execution_error,
    ]
    report = benchmark.aggregate_benchmark([valid, *invalid])

    assert valid.status == "VALID"
    assert all(pair.status == "INVALID" for pair in invalid)
    assert conflict.invalid_traces == {
        "clean": "BASELINE_CONFLICT",
        "fault": "ERROR",
    }
    assert report["counts"] == {
        "pairs": {"total": 7, "valid": 1, "invalid": 6},
        "confusion_traces": {"TP": 1, "FN": 0, "FP": 0, "TN": 1},
        "invalid_traces": {
            "BASELINE_CONFLICT": 1,
            "FAULT_NOT_ACTIVATED": 1,
            "NOT_REACHED": 1,
            "UNOBSERVABLE": 1,
            "INSPECTION_ERROR": 1,
            "ERROR": 2,
        },
    }
    assert report["metrics"]["coverage"]["denominator"] == 7
    assert report["metrics"]["micro_detection_rate"]["denominator"] == 1


def _pair(
    pair_id: str,
    case_index: int,
    clean_detected: bool,
    fault_detected: bool,
) -> benchmark.PairScore:
    fault_id, transitions, finding = FAULT_CASES[case_index]
    clean_pass = artifact(finding) if clean_detected else artifact()
    fault_pass = artifact(finding) if fault_detected else artifact()
    return benchmark.score_pair(
        pair_id,
        benchmark.TraceEvaluation.clean(f"{pair_id}-clean", fault_id, transitions, [clean_pass] * 3),
        benchmark.TraceEvaluation.fault(f"{pair_id}-fault", fault_id, transitions, [fault_pass] * 3),
    )


def test_macro_is_equal_weighted_across_faults_while_micro_uses_raw_traces() -> None:
    report = benchmark.aggregate_benchmark(
        [
            _pair("a-1", 0, False, True),
            _pair("a-2", 0, True, False),
            _pair("b-1", 1, False, True),
        ]
    )

    assert report["counts"]["confusion_traces"] == {"TP": 2, "FN": 1, "FP": 1, "TN": 2}
    assert report["metrics"]["macro_detection_rate"]["value"] == 0.75
    assert "wilson_95" not in report["metrics"]["macro_detection_rate"]
    assert report["metrics"]["micro_detection_rate"]["value"] == pytest.approx(2 / 3)
    assert report["metrics"]["precision"]["value"] == pytest.approx(2 / 3)
    assert report["metrics"]["specificity"]["value"] == pytest.approx(2 / 3)
    assert report["metrics"]["clean_false_positive_rate"]["value"] == pytest.approx(1 / 3)
    assert report["metrics"]["paired_success_rate"]["value"] == pytest.approx(2 / 3)
    assert report["per_fault"]["health_ratio_out_of_range"]["detection_rate"]["value"] == 0.5
    assert report["per_fault"]["relative_position_mismatch"]["detection_rate"]["value"] == 1.0


def test_inspection_agreement_uses_all_three_slots_and_includes_split_error_traces() -> None:
    fault_id, transitions, finding = FAULT_CASES[0]
    clean = benchmark.TraceEvaluation.clean("clean", fault_id, transitions, [artifact()] * 3)
    decided = benchmark.score_pair(
        "decided",
        clean,
        benchmark.TraceEvaluation.fault(
            "fault-decided", fault_id, transitions, [artifact(finding), None, artifact(finding)]
        ),
    )
    split = benchmark.score_pair(
        "split",
        clean,
        benchmark.TraceEvaluation.fault(
            "fault-split", fault_id, transitions, [artifact(finding), None, artifact()]
        ),
    )

    report = benchmark.aggregate_benchmark([decided, split])

    assert report["metrics"]["inspection_agreement"] == {
        "numerator": 9,
        "denominator": 12,
        "value": 0.75,
        "wilson_95": {
            "low": pytest.approx(0.4676946651),
            "high": pytest.approx(0.9110583316),
        },
    }


def test_wilson_95_interval_uses_literal_known_values_and_handles_zero_denominator() -> None:
    assert benchmark.wilson_interval(2, 3) == pytest.approx((0.2076596008, 0.9385080553))
    assert benchmark.wilson_interval(0, 0) == (None, None)
    rate = benchmark.proportion(2, 3)
    assert rate == {
        "numerator": 2,
        "denominator": 3,
        "value": pytest.approx(2 / 3),
        "wilson_95": {
            "low": pytest.approx(0.2076596008),
            "high": pytest.approx(0.9385080553),
        },
    }


def test_json_and_korean_markdown_present_the_same_trace_level_metrics() -> None:
    pairs = [
        _pair("a-1", 0, False, True),
        _pair("a-2", 0, True, False),
        _pair("b-1", 1, False, True),
    ]
    report = benchmark.build_benchmark_report(
        pairs,
        metadata={"campaign_id": "campaign-1", "inspection_model": "fixed-model"},
    )
    markdown = benchmark.render_benchmark_markdown(report)
    encoded = benchmark.benchmark_report_json(report)

    assert report["schema_version"] == "qa-detection-benchmark/v1"
    assert json.loads(encoded) == report
    assert "추적 단위 혼동 행렬" in markdown
    assert "| Fault trace | TP (2) | FN (1) |" in markdown
    assert "| Clean trace | FP (1) | TN (2) |" in markdown
    assert "| Macro detection rate | 75.00% | 2 faults |" in markdown
    assert "| Micro detection rate | 66.67% | 2/3 | 20.77%–93.85% |" in markdown
    assert "| health_ratio_out_of_range | 1 | 1 | 1 | 1 | 50.00% (1/2; 9.45%–90.55%) | 50.00% (1/2; 9.45%–90.55%) | 50.00% (1/2; 9.45%–90.55%) |" in markdown
    assert "| relative_position_mismatch | 1 | 0 | 0 | 1 | 100.00% (1/1; 20.65%–100.00%) | 0.00% (0/1; 0.00%–79.35%) | 100.00% (1/1; 20.65%–100.00%) |" in markdown
    assert "campaign-1" in markdown
