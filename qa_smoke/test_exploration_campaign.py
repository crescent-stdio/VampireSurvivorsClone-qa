from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from . import cli as cli_module
from . import exploration_campaign as exploration
from .detection_campaign import CampaignContractError, InspectionResponse


def observation(
    identifier: str,
    *,
    phase: str = "active_gameplay",
    event: str = "",
) -> dict[str, Any]:
    return {
        "observation_id": identifier,
        "phase": phase,
        "player": {
            "present": True,
            "health": 10.0,
            "max_health": 10.0,
            "health_ratio": 1.0,
            "exp": 0.0,
            "next_level_exp": 10.0,
            "exp_ratio": 0.0,
            "level": 1,
        },
        "player_view": {
            "health": 10.0,
            "max_health": 10.0,
            "health_ratio": 1.0,
            "exp": 0.0,
            "next_level_exp": 10.0,
            "exp_ratio": 0.0,
        },
        "world": {"qa_entities": []},
        "progress": {"level_time": 1.0, "damage_dealt": 1, "damage_taken": 0},
        "menu": {},
        "event_state": {"type": event} if event else {},
    }


def transition(
    index: int,
    *,
    candidate_id: str = "",
    statement: str = "",
    phase: str = "active_gameplay",
    event: str = "",
) -> dict[str, Any]:
    identifier = f"obs-{index:04d}"
    decision: dict[str, Any] = {
        "tool": "game",
        "action": "direct_steer",
        "arguments": {"x": 1.0, "y": 0.0, "duration": 1.0},
    }
    if candidate_id:
        decision.update(
            {
                "qa_observation": statement,
                "reflection": {
                    "status": "unexpected",
                    "summary": statement,
                    "evidence_refs": [identifier],
                    "candidate_id": candidate_id,
                    "reproduction_attempted": False,
                },
                "goal": "must-not-reach-inspector",
                "source_code": "must-not-reach-inspector",
            }
        )
    return {
        "step": index,
        "decision": decision,
        "observation": observation(identifier, phase=phase, event=event),
    }


def behavior_finding(
    rule: str,
    reference: str = "obs-0000",
    *,
    statement: str = "Progress stopped unexpectedly.",
) -> dict[str, Any]:
    return {
        "kind": "behavior",
        "rule": rule,
        "expected_value": "progress advances",
        "observed_value": "progress stopped",
        "statement": statement,
        "evidence_refs": [reference],
    }


def artifact(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "qa-inspection/v2", "findings": list(findings)}


def make_record(
    mission_id: str,
    seed: int,
    *,
    transitions: list[dict[str, Any]],
    passes: list[dict[str, Any] | None] | None = None,
    invariant_validations: list[dict[str, Any]] | None = None,
    launch_faults: tuple[str, ...] = (),
    execution_status: str = "completed",
    coverage_status: str = "reached",
) -> exploration.ExplorationTraceRecord:
    spec = next(
        item
        for item in exploration.build_track_a_schedule()
        if item.mission_id == mission_id and item.seed == seed
    )
    return exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id=f"opaque-{mission_id}-{seed}",
        result=exploration.ExplorationEpisodeResult(
            transitions=transitions,
            execution_status=execution_status,
            coverage_status=coverage_status,
            launch_faults=launch_faults,
            invariant_validations=invariant_validations or [],
        ),
        inspection_passes=passes or [artifact(), artifact(), artifact()],
    )


def test_fixed_track_a_schedule_has_exact_24_clean_llm_runs() -> None:
    schedule = exploration.build_track_a_schedule()

    assert len(schedule) == 24
    assert {item.seed for item in schedule} == {9101, 9102, 9103}
    by_mission = {
        mission_id: [item for item in schedule if item.mission_id == mission_id]
        for mission_id in {item.mission_id for item in schedule}
    }
    assert {key: len(value) for key, value in by_mission.items()} == {
        "core-menu-level1": 3,
        "core-combat-survival": 3,
        "core-progression-upgrades": 3,
        "core-items-chests-inventory": 3,
        "core-death-restart-isolation": 3,
        "long-sustained-combat": 3,
        "long-multi-level-growth-upgrades": 3,
        "long-items-chests-area-effects": 3,
    }
    assert {item.max_steps for item in by_mission["core-menu-level1"]} == {20}
    assert {item.max_steps for item in by_mission["core-combat-survival"]} == {40}
    assert all(
        item.max_steps == 80
        for item in schedule
        if item.mission_id not in {"core-menu-level1", "core-combat-survival"}
    )
    assert all(item.max_simulation_seconds == 240.0 for item in schedule if item.tier == "long")
    assert all(item.variant == "clean" and item.faults == () for item in schedule)
    assert all(item.driver == "llm" and item.model == "gpt-4o-mini" for item in schedule)
    assert all(item.source_tools_enabled is False for item in schedule)
    assert all(item.nested_inspector_enabled is False for item in schedule)


def test_track_a_spec_rejects_faults_hybrid_tools_and_nested_inspection() -> None:
    spec = exploration.build_track_a_schedule()[0]

    for invalid in (
        replace(spec, faults=("health_ratio_out_of_range",)),
        replace(spec, driver="hybrid"),
        replace(spec, source_tools_enabled=True),
        replace(spec, nested_inspector_enabled=True),
    ):
        with pytest.raises(CampaignContractError):
            exploration.validate_track_a_spec(invalid)


def test_aggregation_retains_one_of_three_and_tracks_planner_inspector_surfaces() -> None:
    shared = behavior_finding("progress-stalled")
    invalid = behavior_finding("invalid-evidence", "unknown-observation")
    record = make_record(
        "core-combat-survival",
        9101,
        transitions=[
            transition(
                0,
                candidate_id="Progress Stalled",
                statement="Progress stopped unexpectedly.",
                event="combat",
            )
        ],
        passes=[artifact(shared, invalid), artifact(), artifact()],
    )

    report = exploration.build_exploration_report([record], metadata={"campaign_id": "test"})

    assert report["summary"]["candidate_count"] == 1
    assert report["surface_counts"] == {
        "planner_only": 0,
        "inspector_only": 0,
        "shared": 1,
        "union": 1,
    }
    candidate = report["candidates"][0]
    assert candidate["surface"] == "shared"
    assert candidate["inspection_agreement_by_trace"] == {
        record.opaque_trace_id: "1/3"
    }
    assert candidate["phase"] == "active_gameplay"
    assert candidate["event"] == "combat"
    assert "unknown-observation" not in json.dumps(report)


def test_candidate_tiers_reproduction_and_priorities_are_deterministic() -> None:
    def records(rule: str, *, reproduced: bool, invariant: bool = False):
        seeds = (9101, 9102) if reproduced else (9101,)
        rows = []
        for seed in seeds:
            finding = behavior_finding(rule)
            validation = (
                [
                    {
                        "category": "behavior",
                        "rule": rule,
                        "field": "",
                        "phase": "active_gameplay",
                        "event": "",
                        "evidence_refs": ["obs-0000"],
                    }
                ]
                if invariant
                else []
            )
            rows.append(
                make_record(
                    "core-combat-survival",
                    seed,
                    transitions=[transition(0)],
                    passes=[artifact(finding), artifact(finding), artifact()],
                    invariant_validations=validation,
                )
            )
        return rows

    all_records = [
        *records("unrecoverable-hang", reproduced=False),
        *records("progression-state-loss", reproduced=True, invariant=True),
        *records("combat-ui-numeric-damage", reproduced=True),
        *records("minor-animation-jitter", reproduced=False),
    ]
    candidates = {
        candidate["rule"]: candidate
        for candidate in exploration.build_exploration_report(
            all_records, metadata={}
        )["candidates"]
    }

    assert candidates["unrecoverable-hang"]["priority"] == "P0"
    assert candidates["progression-state-loss"]["priority"] == "P1"
    assert candidates["progression-state-loss"]["tier"] == "validated-invariant candidate"
    assert candidates["combat-ui-numeric-damage"]["priority"] == "P2"
    assert candidates["minor-animation-jitter"]["priority"] == "P3"
    assert candidates["minor-animation-jitter"]["tier"] == "evidence-linked candidate"
    assert candidates["progression-state-loss"]["reproduced"] is True

    different_missions = [
        make_record(
            "core-combat-survival",
            9101,
            transitions=[transition(0)],
            passes=[artifact(behavior_finding("same-rule")), artifact(), artifact()],
        ),
        make_record(
            "core-progression-upgrades",
            9102,
            transitions=[transition(0)],
            passes=[artifact(behavior_finding("same-rule")), artifact(), artifact()],
        ),
    ]
    assert exploration.build_exploration_report(different_missions, metadata={})[
        "candidates"
    ][0]["reproduced"] is False


def test_harness_failures_are_excluded_and_zero_report_avoids_bug_free_claims() -> None:
    injected = make_record(
        "core-combat-survival",
        9101,
        transitions=[transition(0)],
        passes=[artifact(behavior_finding("must-be-excluded")), artifact(), artifact()],
        launch_faults=("health_ratio_out_of_range",),
    )
    coverage = make_record(
        "core-combat-survival",
        9102,
        transitions=[transition(0)],
        passes=[artifact(behavior_finding("also-excluded")), artifact(), artifact()],
        coverage_status="not_reached",
    )
    model_failure = make_record(
        "core-combat-survival",
        9103,
        transitions=[transition(0)],
        passes=[artifact(behavior_finding("model-excluded")), artifact(), artifact()],
    )
    model_failure = replace(
        model_failure,
        result=replace(
            model_failure.result,
            execution_status="infrastructure_error",
            error="LLMContractError: request failed",
        ),
    )
    partial_inspection = make_record(
        "core-progression-upgrades",
        9101,
        transitions=[transition(0)],
        passes=[artifact(behavior_finding("partial-excluded")), None, artifact()],
    )
    empty_trace = make_record(
        "core-progression-upgrades",
        9102,
        transitions=[],
    )

    report = exploration.build_exploration_report(
        [injected, coverage, model_failure, partial_inspection, empty_trace],
        metadata={},
    )
    markdown = exploration.render_exploration_markdown(report)
    serialized = exploration.exploration_report_json(report)

    assert report["summary"]["candidate_count"] == 0
    assert report["summary"]["eligible_gameplay_traces"] == 0
    assert {failure["kind"] for failure in report["harness_failures"]} == {
        "injected_fault_config",
        "coverage_failure",
        "model_failure",
        "inspection_failure",
        "schema_failure",
    }
    assert all("trace_id" in failure for failure in report["harness_failures"])
    assert "관찰된 버그 후보가 없습니다" in markdown
    assert "게임에 버그가 없음을 의미하지 않습니다" in markdown
    forbidden = ("bug-free", "confirmed bug", "true-positive rate", "tpr")
    for marker in forbidden:
        assert marker not in f"{serialized}\n{markdown}".lower()
    assert json.loads(serialized) == report


class FakeBackend:
    def __init__(self) -> None:
        self.specs: list[exploration.ExplorationEpisodeSpec] = []

    def run_autonomous(
        self,
        spec: exploration.ExplorationEpisodeSpec,
        output_dir: Path,
    ) -> exploration.ExplorationEpisodeResult:
        self.specs.append(spec)
        return exploration.ExplorationEpisodeResult(
            transitions=[
                transition(
                    0,
                    candidate_id="private-planner-channel",
                    statement="No anomaly was actually observed.",
                )
            ],
            coverage_status="reached",
            launch_faults=(),
        )


class FakeInspector:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def inspect(self, system_prompt: str, payload: dict[str, Any]) -> InspectionResponse:
        self.requests.append(json.loads(json.dumps(payload)))
        return InspectionResponse(
            raw_response=artifact(),
            usage={"llm_http_attempts": 1, "llm_completion_requests": 1},
        )


def campaign_config(tmp_path: Path, *, resume: bool = False):
    build = tmp_path / "game-build"
    if not build.exists():
        build.write_bytes(b"stable-clean-build")
    return exploration.ExplorationCampaignConfig(
        build=build,
        project_root=Path(__file__).resolve().parents[1],
        output=tmp_path / "exploration",
        headless=True,
        quiet=True,
        resume=resume,
    )


def test_campaign_runs_blind_clean_schedule_and_exactly_resumes_with_tamper_repair(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    inspector = FakeInspector()
    settings = campaign_config(tmp_path)

    result = exploration.run_exploration_campaign(
        settings,
        backend=backend,
        inspector=inspector,
    )

    assert result.resumed is False
    assert len(backend.specs) == 24
    assert len(inspector.requests) == 24 * 3
    assert all(spec.faults == () for spec in backend.specs)
    assert all(spec.driver == "llm" for spec in backend.specs)
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["schema_version"] == "qa-campaign-manifest/v1"
    assert manifest["track"] == "A"
    assert manifest["counts"]["scheduled_traces"] == 24
    assert manifest["limits"]["planned_inspection_calls"] == 189
    assert all(trace["launch"]["faults"] == [] for trace in manifest["traces"])
    launch_manifests = list(settings.output.glob("traces/**/launch-manifest.json"))
    assert len(launch_manifests) == 24
    assert all(
        json.loads(path.read_text())["launch"]
        == {
            "faults": [],
            "nested_inspector_enabled": False,
            "policy": "llm",
            "source_tools_enabled": False,
        }
        for path in launch_manifests
    )

    serialized_requests = json.dumps(inspector.requests, ensure_ascii=False).lower()
    for private in ("private-planner-channel", "must-not-reach-inspector", "goal", "source_code"):
        assert private not in serialized_requests

    resumed = exploration.run_exploration_campaign(
        replace(settings, resume=True),
        backend=backend,
        inspector=inspector,
    )
    assert resumed.resumed is True
    assert len(backend.specs) == 24
    assert len(inspector.requests) == 24 * 3

    episode = next(settings.output.glob("traces/**/episode-result.json"))
    episode.write_text("{}\n", encoding="utf-8")
    exploration.run_exploration_campaign(
        replace(settings, resume=True),
        backend=backend,
        inspector=inspector,
    )
    assert len(backend.specs) == 25
    assert len(inspector.requests) == 24 * 3 + 3


def test_campaign_rejects_nonempty_output_without_resume_and_hash_mismatch(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    inspector = FakeInspector()
    settings = campaign_config(tmp_path)
    exploration.run_exploration_campaign(settings, backend=backend, inspector=inspector)

    with pytest.raises(CampaignContractError, match="resume"):
        exploration.run_exploration_campaign(
            settings,
            backend=backend,
            inspector=inspector,
        )
    with pytest.raises(CampaignContractError, match="campaign hash mismatch"):
        exploration.run_exploration_campaign(
            replace(settings, resume=True, headless=False),
            backend=backend,
            inspector=inspector,
        )


def test_resume_hash_includes_the_rubric_file(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "config" / "qa-detection-rubric.json"
    shutil.copyfile(source, config_dir / source.name)
    settings = replace(campaign_config(tmp_path), project_root=project_root)
    backend = FakeBackend()
    inspector = FakeInspector()
    exploration.run_exploration_campaign(settings, backend=backend, inspector=inspector)

    (config_dir / source.name).write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(CampaignContractError, match="campaign hash mismatch"):
        exploration.run_exploration_campaign(
            replace(settings, resume=True),
            backend=backend,
            inspector=inspector,
        )


def test_cli_parses_explore_options_and_keeps_benchmark_detection_stable(tmp_path: Path) -> None:
    parsed = cli_module.parse_cli(
        [
            "explore",
            "--build",
            str(tmp_path / "game"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "out"),
            "--api-url",
            "https://example.invalid/v1/chat/completions",
            "--headless",
            "--quiet",
            "--resume",
        ]
    )

    assert parsed.command == "explore"
    assert parsed.resume is True
    assert parsed.output == tmp_path / "out"
    assert parsed.api_url == "https://example.invalid/v1/chat/completions"
    assert not hasattr(parsed, "model")

    benchmark = cli_module.parse_cli(
        [
            "benchmark-detection",
            "--build",
            str(tmp_path / "game"),
            "--output",
            str(tmp_path / "benchmark"),
        ]
    )
    assert benchmark.command == "benchmark-detection"
    assert not hasattr(benchmark, "resume")
