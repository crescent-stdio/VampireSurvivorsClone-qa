from __future__ import annotations

from . import inspector
from . import detection
from .inspector import build_inspection_payload
from . import run as run_module


def test_inspection_payload_keeps_only_public_player_view_telemetry() -> None:
    """A display mismatch remains inspectable without exposing evaluator material."""
    payload = build_inspection_payload(
        [
            {
                "observation": {
                    "observation_id": "obs-0001",
                    "player": {"health": 96.0, "max_health": 100.0, "exp": 40.0},
                    "player_view": {
                        "health": 95.0,
                        "max_health": 100.0,
                        "health_ratio": 0.95,
                        "exp": 39.0,
                        "next_level_exp": 50.0,
                        "exp_ratio": 0.78,
                        "level": 4,
                        "fault_id": "health_ratio_out_of_range",
                    },
                    "nested": {
                        "ground_truth": {"expected_behavior": "private"},
                        "recent_logs": [
                            "Injected health_ratio_out_of_range via Assets/Scripts/QA/QaFaultInjection.cs",
                        ],
                    },
                    "recent_logs": [
                        "Injected health_ratio_out_of_range via Assets/Scripts/QA/QaFaultInjection.cs",
                    ],
                }
            }
        ]
    )

    observation = payload["observations"][0]
    assert observation.get("player_view") == {
        "health": 95.0,
        "max_health": 100.0,
        "health_ratio": 0.95,
        "exp": 39.0,
        "next_level_exp": 50.0,
        "exp_ratio": 0.78,
    }
    serialized = str(payload)
    for private_value in (
        "health_ratio_out_of_range",
        "Injected",
        "Assets/Scripts/QA/QaFaultInjection.cs",
        "ground_truth",
        "expected_behavior",
    ):
        assert private_value not in serialized


def test_v2_normalization_requires_explicit_numeric_comparison_and_reads_v1() -> None:
    """A finding must carry a numeric relation, while old artifacts remain usable."""
    assert hasattr(inspector, "normalize_inspection_artifact")
    v2 = inspector.normalize_inspection_artifact(
        {
            "schema_version": inspector.INSPECTION_SCHEMA_V2,
            "findings": [
                {
                    "kind": "numeric",
                    "field": "player_view.health",
                    "comparison": "!=",
                    "expected_value": 96.0,
                    "observed_value": 95.0,
                    "statement": "Displayed health differs from the raw value.",
                    "evidence_refs": ["obs-0001"],
                },
                {
                    "kind": "numeric",
                    "field": "player_view.health",
                    "expected_value": 96.0,
                    "observed_value": 95.0,
                    "statement": "Missing comparison is invalid.",
                    "evidence_refs": ["obs-0001"],
                },
            ],
        }
    )
    legacy = inspector.normalize_inspection_artifact(
        {
            "findings": [
                {
                    "field": "player.health_ratio",
                    "computed_value": 0.96,
                    "reported_value": 1.25,
                    "statement": "The values disagree.",
                    "evidence_refs": ["obs-0002"],
                }
            ]
        }
    )

    assert v2["schema_version"] == inspector.INSPECTION_SCHEMA_V2
    assert v2["findings"] == [
        {
            "kind": "numeric",
            "field": "player_view.health",
            "comparison": "!=",
            "expected_value": 96.0,
            "observed_value": 95.0,
            "statement": "Displayed health differs from the raw value.",
            "evidence_refs": ["obs-0001"],
        }
    ]
    assert legacy["findings"][0]["comparison"] == "!="
    assert legacy["findings"][0]["expected_value"] == 0.96
    assert legacy["findings"][0]["observed_value"] == 1.25


def test_inspection_chunks_have_stable_two_observation_overlap_and_deduplicate() -> None:
    """Chunking is generic and overlapping model findings collapse without oracle input."""
    transitions = [
        {"observation": {"observation_id": f"obs-{index:02d}", "player": {}}}
        for index in range(33)
    ]
    assert hasattr(inspector, "build_inspection_chunks")
    chunks = inspector.build_inspection_chunks(transitions)
    finding = {
        "kind": "numeric",
        "field": "player_view.exp",
        "comparison": "!=",
        "expected_value": 40.0,
        "observed_value": 39.0,
        "statement": "Displayed experience differs from the raw value.",
        "evidence_refs": ["obs-30"],
    }
    normalized = inspector.normalize_inspection_artifact(
        {"schema_version": inspector.INSPECTION_SCHEMA_V2, "findings": [finding, dict(finding)]}
    )

    assert [chunk["chunk_id"] for chunk in chunks] == [
        "inspection-chunk-000000-000031",
        "inspection-chunk-000030-000032",
    ]
    assert [len(chunk["observations"]) for chunk in chunks] == [32, 3]
    assert chunks[0]["observations"][-2:] == chunks[1]["observations"][:2]
    assert normalized["findings"] == [finding]


def test_scenario_evaluation_runs_disable_source_tools() -> None:
    """Scenario-owned evaluation cannot re-enable source access through defaults."""
    args = run_module.parse_args(
        [
            "--game-exe", "player.app",
            "--output", "artifacts",
            "--mode", "qa",
            "--scenario", "easy-health-ratio",
            "--max-source-steps", "6",
        ]
    )

    assert args.max_source_steps == 0


def test_inspection_payload_removes_private_key_variants_and_unknown_injection_text() -> None:
    """Nested evaluator aliases and injection logs are never model-visible."""
    payload = build_inspection_payload(
        [
            {
                "observation": {
                    "observation_id": "obs-0003",
                    "player": {
                        "health": 10.0,
                        "oracle_data": {"verdict": "private"},
                        "context_reference": "src/hidden/Rules.cs",
                    },
                    "recent_logs": ["error: Injected developer-only QA state"],
                }
            }
        ]
    )

    serialized = str(payload)
    for private_value in (
        "oracle_data",
        "src/hidden/Rules.cs",
        "Injected developer-only QA state",
    ):
        assert private_value not in serialized


def test_v2_numeric_finding_is_scored_with_its_explicit_comparison() -> None:
    """The scorer consumes the v2 fields produced by the inspector, not v1 aliases."""
    result = detection.score_agent_detection(
        fault_id="health_ratio_out_of_range",
        policy="heuristic",
        has_agent_text_channel=True,
        trace_completeness="complete",
        oracle_verdict="fail",
        transitions=[],
        fault_refs=["obs-0001"],
        inspection={
            "schema_version": inspector.INSPECTION_SCHEMA_V2,
            "findings": [
                {
                    "kind": "numeric",
                    "field": "player.health_ratio",
                    "comparison": "!=",
                    "expected_value": 0.96,
                    "observed_value": 1.25,
                    "statement": "The reported ratio differs from the computed ratio.",
                    "evidence_refs": ["obs-0001"],
                }
            ],
        },
    )

    assert result.status == "match"
    assert result.matched_surfaces == ["inspection"]
