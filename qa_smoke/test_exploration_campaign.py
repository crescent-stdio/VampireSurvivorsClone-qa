from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from . import cli as cli_module
from . import detection_campaign as detection_campaign
from . import exploration_campaign as exploration
from . import run as run_module
from .adapters import VampireSurvivorsAdapter
from .detection_campaign import (
    CampaignContractError,
    CampaignExecutionError,
    InspectionResponse,
)


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


def numeric_finding(
    field: str,
    reference: str = "obs-0000",
    *,
    comparison: str = "!=",
) -> dict[str, Any]:
    return {
        "kind": "numeric",
        "field": field,
        "comparison": comparison,
        "expected_value": 1.0,
        "observed_value": 1.25,
        "statement": f"{field} does not match its expected value.",
        "evidence_refs": [reference],
    }


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


def test_poc_track_a_schedule_has_exact_selected_missions_and_seeds() -> None:
    schedule = exploration.build_track_a_schedule("poc")

    assert len(schedule) == 6
    assert {item.seed for item in schedule} == {9101, 9102}
    assert {item.mission_id for item in schedule} == {
        "core-combat-survival",
        "core-progression-upgrades",
        "core-death-restart-isolation",
    }


def test_track_a_profile_changes_campaign_identity(tmp_path: Path) -> None:
    full = campaign_config(tmp_path)
    poc = replace(full, profile="poc")
    build_hash = exploration.hash_path(full.build)

    assert exploration._campaign_hash(full, build_hash) != exploration._campaign_hash(
        poc, build_hash
    )


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
        "runtime_oracle": 0,
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


def test_numeric_planner_and_inspector_findings_share_only_an_exact_structured_key() -> None:
    shared_record = make_record(
        "core-combat-survival",
        9101,
        transitions=[
            transition(
                0,
                candidate_id="numeric:ne:player.health_ratio",
                statement="player.health_ratio expected 1.0 but observed 1.25.",
                event="combat",
            )
        ],
        passes=[
            artifact(numeric_finding("player.health_ratio")),
            artifact(),
            artifact(),
        ],
    )

    shared_report = exploration.build_exploration_report([shared_record], metadata={})

    assert shared_report["surface_counts"] == {
        "planner_only": 0,
        "inspector_only": 0,
        "shared": 1,
        "runtime_oracle": 0,
        "union": 1,
    }
    assert shared_report["candidates"][0]["category"] == "numeric"
    assert shared_report["candidates"][0]["rule"] == "numeric-not-equal"
    assert shared_report["candidates"][0]["field"] == "player.health_ratio"

    near_miss_record = make_record(
        "core-combat-survival",
        9101,
        transitions=[
            transition(
                0,
                candidate_id="numeric:ne:player.health_ratio",
                statement="player.health_ratio expected 1.0 but observed 1.25.",
                event="combat",
            )
        ],
        passes=[
            artifact(
                numeric_finding("player.exp_ratio"),
                numeric_finding("player.health_ratio", comparison=">"),
            ),
            artifact(),
            artifact(),
        ],
    )

    near_miss_report = exploration.build_exploration_report(
        [near_miss_record], metadata={}
    )

    assert near_miss_report["surface_counts"] == {
        "planner_only": 1,
        "inspector_only": 2,
        "shared": 0,
        "runtime_oracle": 0,
        "union": 3,
    }


@pytest.mark.parametrize(
    "field_name",
    [
        "world.threat_entities[0].relative_x",
        "inventory.abilities[0].level",
    ],
)
def test_numeric_shared_surface_accepts_strict_indexed_field_paths(
    field_name: str,
) -> None:
    record = make_record(
        "core-combat-survival",
        9101,
        transitions=[
            transition(
                0,
                candidate_id=f"numeric:ne:{field_name}",
                statement=f"{field_name} differs from the expected value.",
                event="combat",
            )
        ],
        passes=[artifact(numeric_finding(field_name)), artifact(), artifact()],
    )

    report = exploration.build_exploration_report([record], metadata={})

    assert report["surface_counts"] == {
        "planner_only": 0,
        "inspector_only": 0,
        "shared": 1,
        "runtime_oracle": 0,
        "union": 1,
    }
    assert report["candidates"][0]["field"] == field_name


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


def test_priority_uses_whole_reason_tokens_instead_of_substrings() -> None:
    record = make_record(
        "core-combat-survival",
        9101,
        transitions=[transition(0), transition(1)],
        passes=[
            artifact(
                behavior_finding(
                    "health-did-not-change",
                    statement="Health did not change after the hit.",
                ),
                behavior_finding(
                    "runtime-hang",
                    "obs-0001",
                    statement="The player entered a hang state.",
                ),
                behavior_finding(
                    "render-freeze",
                    "obs-0001",
                    statement="The rendered game entered a freeze state.",
                ),
            ),
            artifact(),
            artifact(),
        ],
    )

    candidates = {
        candidate["rule"]: candidate
        for candidate in exploration.build_exploration_report([record], metadata={})[
            "candidates"
        ]
    }

    assert candidates["health-did-not-change"]["priority"] != "P0"
    assert candidates["runtime-hang"]["priority"] == "P0"
    assert candidates["render-freeze"]["priority"] == "P0"


def test_duplicate_findings_merge_evidence_and_statements_without_extra_votes() -> None:
    first = numeric_finding("player_view.health_ratio")
    first["statement"] = "First observation statement."
    second = numeric_finding(
        "player_view.health_ratio",
        "obs-0001",
    )
    second["observed_value"] = 1.5
    second["statement"] = "Second observation statement."
    record = make_record(
        "core-combat-survival",
        9101,
        transitions=[transition(0), transition(1)],
        passes=[artifact(first, second), artifact(), artifact()],
    )

    candidate = exploration.build_exploration_report([record], metadata={})[
        "candidates"
    ][0]

    assert candidate["inspection_agreement_by_trace"] == {
        record.opaque_trace_id: "1/3"
    }
    assert candidate["evidence"][0]["evidence_refs"] == ["obs-0000", "obs-0001"]
    assert "First observation statement." in " ".join(candidate["statements"])
    assert "Second observation statement." in " ".join(candidate["statements"])


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
        passes=[artifact(), artifact(), artifact()],
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


class ResultBackend:
    def __init__(self, result: exploration.ExplorationEpisodeResult) -> None:
        self.result = result
        self.calls = 0

    def run_autonomous(self, spec, output_dir):
        self.calls += 1
        return self.result


class RaisingBackend:
    def run_autonomous(self, spec, output_dir):
        raise RuntimeError("provider-secret-body-must-not-be-retained")


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


@pytest.mark.parametrize(
    "result",
    [
        exploration.ExplorationEpisodeResult(
            transitions=[transition(0)],
            launch_faults=("health_ratio_out_of_range",),
        ),
        exploration.ExplorationEpisodeResult(
            transitions=[transition(0)],
            execution_status="bridge_failure",
            error="BridgeProcessError",
        ),
        exploration.ExplorationEpisodeResult(
            transitions=[transition(0)],
            execution_status="model_failure",
            error="LLMTransportError",
        ),
        exploration.ExplorationEpisodeResult(
            transitions=[transition(0)],
            execution_status="schema_error",
            error="ValueError",
        ),
    ],
)
def test_dirty_or_required_execution_failure_makes_campaign_incomplete(
    tmp_path: Path,
    result: exploration.ExplorationEpisodeResult,
) -> None:
    settings = campaign_config(tmp_path)

    with pytest.raises((CampaignContractError, CampaignExecutionError)):
        exploration.run_exploration_campaign(
            settings,
            backend=ResultBackend(result),
            inspector=FakeInspector(),
        )

    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert not (settings.output / "exploration-report.json").exists()
    assert not (settings.output / "exploration-report.ko.md").exists()


def test_backend_failure_artifacts_keep_only_a_safe_error_type(tmp_path: Path) -> None:
    settings = campaign_config(tmp_path)

    with pytest.raises(CampaignExecutionError):
        exploration.run_exploration_campaign(
            settings,
            backend=RaisingBackend(),
            inspector=FakeInspector(),
        )

    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            settings.output / "campaign-manifest.json",
            settings.output / "checkpoint.json",
            next(settings.output.glob("traces/**/episode-result.json")),
        )
    )
    assert "RuntimeError" in public_artifacts
    assert "provider-secret" not in public_artifacts


def test_exploration_episode_serialization_discards_free_form_error_details() -> None:
    serialized = exploration.ExplorationEpisodeResult(
        transitions=[],
        execution_status="infrastructure_error",
        coverage_status="error",
        error="RuntimeError: token=fake-secret at /private/provider",
    ).to_dict()

    assert serialized["error"] == "RuntimeError"
    assert "fake-secret" not in json.dumps(serialized)
    assert "/private/provider" not in json.dumps(serialized)


def test_coverage_not_reached_remains_a_separately_reported_outcome(
    tmp_path: Path,
) -> None:
    settings = campaign_config(tmp_path)
    result = exploration.run_exploration_campaign(
        settings,
        backend=ResultBackend(
            exploration.ExplorationEpisodeResult(
                transitions=[transition(0)],
                coverage_status="not_reached",
            )
        ),
        inspector=FakeInspector(),
    )

    manifest = json.loads(result.manifest_path.read_text())
    report = json.loads(result.report_path.read_text())
    assert manifest["status"] == "complete"
    assert report["summary"]["candidate_count"] == 0
    assert report["summary"]["eligible_gameplay_traces"] == 0
    assert {item["kind"] for item in report["harness_failures"]} == {
        "coverage_failure"
    }


def test_coverage_gap_does_not_discard_an_evidence_valid_game_candidate() -> None:
    record = make_record(
        "long-multi-level-growth-upgrades",
        9101,
        transitions=[transition(0)],
        passes=[artifact(behavior_finding("runtime-crash")), artifact(), artifact()],
        coverage_status="not_reached",
    )

    report = exploration.build_exploration_report([record], metadata={})

    assert report["summary"]["eligible_gameplay_traces"] == 0
    assert report["summary"]["candidate_evidence_traces"] == 1
    assert report["summary"]["candidate_count"] == 1
    assert report["candidates"][0]["priority"] == "P0"
    assert {item["kind"] for item in report["harness_failures"]} == {
        "coverage_failure"
    }


def test_failed_resume_archives_stale_complete_reports(tmp_path: Path) -> None:
    settings = campaign_config(tmp_path)
    exploration.run_exploration_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    episode = next(settings.output.glob("traces/**/episode-result.json"))
    episode.write_text("{}\n", encoding="utf-8")

    with pytest.raises(CampaignContractError):
        exploration.run_exploration_campaign(
            replace(settings, resume=True),
            backend=ResultBackend(
                exploration.ExplorationEpisodeResult(
                    transitions=[transition(0)],
                    launch_faults=("dirty",),
                )
            ),
            inspector=FakeInspector(),
        )

    assert not (settings.output / "exploration-report.json").exists()
    assert not (settings.output / "exploration-report.ko.md").exists()
    assert list(settings.output.glob(".superseded-results/**/exploration-report.json"))


def test_report_publication_failure_leaves_only_an_incomplete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    original_write_text = Path.write_text

    def fail_markdown(self, data, *args, **kwargs):
        if self.name == "exploration-report.ko.md":
            raise OSError("publication failed")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_markdown)

    with pytest.raises(OSError):
        exploration.run_exploration_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert not (settings.output / "exploration-report.json").exists()
    assert not (settings.output / "exploration-report.ko.md").exists()
    assert list(settings.output.glob(".superseded-results/**/exploration-report.json"))


def test_archive_failure_invalidates_stale_complete_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    exploration.run_exploration_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )

    def fail_archive(output: Path) -> None:
        raise OSError("archive failed at /private/qa token=fake-secret")

    monkeypatch.setattr(exploration, "_archive_published_results", fail_archive)

    with pytest.raises(OSError):
        exploration.run_exploration_campaign(
            replace(settings, resume=True),
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"] == {
        "status": "publication_cleanup_failure",
        "error_type": "OSError",
    }
    assert not (settings.output / "exploration-report.json").exists()
    assert not (settings.output / "exploration-report.ko.md").exists()
    assert "/private/qa" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_initialization_running_manifest_failure_invalidates_stale_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    exploration.run_exploration_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )

    def fail_running_manifest(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("running manifest failed at /private/qa token=fake-secret")

    monkeypatch.setattr(exploration, "_write_running_manifest", fail_running_manifest)

    with pytest.raises(OSError):
        exploration.record_exploration_initialization_failure(
            replace(settings, resume=True),
            ValueError("model initialization failed"),
        )

    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"] == {
        "status": "publication_cleanup_failure",
        "error_type": "OSError",
    }
    assert not (settings.output / "exploration-report.json").exists()
    assert not (settings.output / "exploration-report.ko.md").exists()
    assert "/private/qa" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_explore_cli_returns_nonzero_for_incomplete_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QA_API_KEY", "test-key")
    settings = campaign_config(tmp_path)
    parsed = cli_module.parse_cli(
        [
            "explore",
            "--build",
            str(settings.build),
            "--project-root",
            str(settings.project_root),
            "--output",
            str(settings.output),
        ]
    )

    def fail_campaign(*args, **kwargs):
        raise CampaignExecutionError("required unit failed")

    monkeypatch.setattr(exploration, "run_exploration_campaign", fail_campaign)

    assert cli_module._explore(parsed) == 2


def test_explore_cli_sanitizes_publication_exception_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("QA_API_KEY", "test-key")
    settings = campaign_config(tmp_path)
    exploration.run_exploration_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    parsed = cli_module.parse_cli(
        [
            "explore",
            "--build",
            str(settings.build),
            "--project-root",
            str(settings.project_root),
            "--output",
            str(settings.output),
            "--resume",
            "--headless",
            "--quiet",
        ]
    )

    def fail_archive(output: Path) -> None:
        raise OSError("archive failed at /private/qa token=fake-secret")

    monkeypatch.setattr(exploration, "_archive_published_results", fail_archive)

    assert cli_module._explore(parsed) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "status": "incomplete",
        "error_type": "OSError",
    }
    assert "Traceback" not in captured.err
    assert "/private/qa" not in captured.err
    assert "fake-secret" not in captured.err
    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    assert json.loads(manifest_text)["failure"]["status"] == (
        "publication_cleanup_failure"
    )
    assert "/private/qa" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_explore_cli_records_safe_incomplete_manifest_on_inspector_init_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = campaign_config(tmp_path)
    parsed = cli_module.parse_cli(
        [
            "explore",
            "--build",
            str(settings.build),
            "--project-root",
            str(settings.project_root),
            "--output",
            str(settings.output),
        ]
    )

    assert cli_module._explore(parsed) == 2
    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"] == {
        "status": "model_failure",
        "error_type": "ValueError",
    }
    assert "QA_API_KEY" not in manifest_text


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
    assert len(manifest["hashes"]["inspection_request_contract"]) == 64
    assert (
        manifest["inspection_request_contract_version"]
        == exploration.INSPECTION_REQUEST_CONTRACT_VERSION
    )
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


def test_campaign_identity_tracks_effective_endpoint_query_and_planner_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    build_hash = exploration.hash_path(settings.build)
    monkeypatch.setenv(
        "QA_API_URL",
        "https://user:secret@example.invalid/v1/chat/completions?api-version=1&token=hidden",
    )
    endpoint_v1 = exploration._campaign_hash(settings, build_hash)
    public_endpoint_v1 = exploration._public_api_endpoint_hash(None)
    monkeypatch.setenv(
        "QA_API_URL",
        "https://user:secret@example.invalid/v1/chat/completions?api-version=2&token=hidden",
    )
    endpoint_v2 = exploration._campaign_hash(settings, build_hash)
    public_endpoint_v2 = exploration._public_api_endpoint_hash(None)

    assert endpoint_v1 != endpoint_v2
    assert public_endpoint_v1 == public_endpoint_v2
    assert "secret" not in public_endpoint_v1
    assert "hidden" not in public_endpoint_v1

    completed = exploration.run_exploration_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    public_manifest = completed.manifest_path.read_text(encoding="utf-8")
    assert "secret" not in public_manifest
    assert "hidden" not in public_manifest
    assert "api-version" not in public_manifest

    prompt_before = exploration._steering_prompt_digest()

    def changed_prompt(self):
        return "changed steering prompt"

    monkeypatch.setattr(
        exploration.LLMPlanner,
        "_planning_system_prompt",
        changed_prompt,
    )
    prompt_after = exploration._steering_prompt_digest()

    assert prompt_before != prompt_after
    assert exploration._campaign_hash(settings, build_hash) != endpoint_v2


def test_steering_identity_includes_all_effective_prompt_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_clear = getattr(
        getattr(exploration, "_steering_dependency_source_hashes", None),
        "cache_clear",
        lambda: None,
    )
    cache_clear()
    called_files: set[str] = set()
    original_file_hash = exploration._file_hash

    def track_file_hash(path: Path) -> str:
        called_files.add(Path(path).name)
        return original_file_hash(path)

    monkeypatch.setattr(exploration, "_file_hash", track_file_hash)
    exploration._steering_prompt_digest()

    assert {
        "charter.py",
        "memory.py",
        "planners.py",
        "reporting.py",
        "run.py",
        "state_channels.py",
    } <= called_files
    assert exploration.STEERING_PROMPT_TEMPLATE_VERSION == "qa-planning/v6"
    cache_clear()


def test_steering_dependency_source_hashes_are_frozen_for_the_loaded_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []
    cache = getattr(exploration, "_steering_dependency_source_hashes", lambda: None)
    getattr(cache, "cache_clear", lambda: None)()

    def count_hash_reads(path: Path) -> str:
        reads.append(path.name)
        return "a" * 64

    monkeypatch.setattr(exploration, "_file_hash", count_hash_reads)

    first = exploration._steering_prompt_digest()
    second = exploration._steering_prompt_digest()

    assert first == second
    assert len(reads) == len(set(reads)) == 6
    getattr(cache, "cache_clear", lambda: None)()


def test_campaign_identity_includes_inspection_normalizer_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    build_hash = exploration.hash_path(settings.build)
    before = exploration._campaign_hash(settings, build_hash)

    monkeypatch.setattr(
        exploration,
        "INSPECTION_NORMALIZER_VERSION",
        "changed-normalizer-version",
        raising=False,
    )

    assert exploration._campaign_hash(settings, build_hash) != before


def test_track_a_exact_identity_binds_inspection_schema_and_request_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = campaign_config(tmp_path)
    build_hash = exploration.hash_path(settings.build)
    baseline_hash = exploration._campaign_hash(settings, build_hash)

    with monkeypatch.context() as schema_patch:
        schema_patch.setattr(
            detection_campaign,
            "FINDINGS_SCHEMA",
            {
                **detection_campaign.FINDINGS_SCHEMA,
                "title": "changed-track-a-inspection-schema",
            },
        )
        assert exploration._campaign_hash(settings, build_hash) != baseline_hash

    checkpoint_path = settings.output / "identity-checkpoint.json"
    exploration.CheckpointStore(checkpoint_path, baseline_hash)
    monkeypatch.setattr(detection_campaign, "MAX_HTTP_ATTEMPTS", 4)
    changed_hash = exploration._campaign_hash(settings, build_hash)
    assert changed_hash != baseline_hash
    with pytest.raises(CampaignContractError, match="campaign hash mismatch"):
        exploration.CheckpointStore(checkpoint_path, changed_hash)


def test_run_arguments_never_persist_the_private_api_endpoint() -> None:
    arguments = run_module._public_run_arguments(
        SimpleNamespace(
            api_url=(
                "https://user:secret@example.invalid/v1/chat/completions"
                "?api-version=2&token=hidden"
            ),
            scenario_definition=None,
            game_exe=Path("/tmp/game"),
            project_root=Path("/tmp/project"),
            output=Path("/tmp/output"),
            policy="llm",
        )
    )

    serialized = json.dumps(arguments)
    assert "api_url" not in arguments
    assert "secret" not in serialized
    assert "hidden" not in serialized


def test_real_run_planner_initialization_report_is_safely_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    output = tmp_path / "planner-init-run"
    args = run_module.parse_args(
        [
            "--game-exe",
            str(tmp_path / "unused-game"),
            "--project-root",
            str(Path(__file__).resolve().parents[1]),
            "--output",
            str(output),
            "--mode",
            "qa",
            "--policy",
            "llm",
            "--model",
            exploration.STEERING_MODEL,
            "--objective",
            "Exercise planner initialization safely.",
            "--quiet",
        ]
    )

    assert run_module.run_session(args) == 1
    report_text = (output / "report.json").read_text()
    report = json.loads(report_text)
    assert report["fatal_error"] == "LLMInitializationError: details redacted"
    assert "QA_API_KEY" not in report_text

    spec = exploration.build_track_a_schedule()[0]
    loaded = exploration.BridgeExplorationBackend._load_result(spec, output)
    assert loaded.execution_status == "model_failure"
    assert loaded.error == "LLMInitializationError"


def test_terminal_exception_details_are_sanitized_across_public_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_message = "timeout at /private/qa/player token=fake-secret"

    class LeakyTimeoutBridge:
        def __init__(self, **kwargs: Any) -> None:
            self.process = SimpleNamespace(returncode=None)
            self.command_count = 0

        def launch(self) -> dict[str, Any]:
            return {"ready": True}

        def command(self, action: str, **parameters: Any) -> dict[str, Any]:
            self.command_count += 1
            state = observation(f"obs-{self.command_count:04d}")
            state.update(
                {
                    "ok": True,
                    "scene": "Level1",
                    "frame": self.command_count,
                    "available_actions": ["wait"],
                }
            )
            state["progress"]["level_time"] = float(self.command_count)
            return state

        def close(self) -> None:
            raise TimeoutError(secret_message)

    class LeakyErrorBridge(LeakyTimeoutBridge):
        def close(self) -> None:
            raise RuntimeError(secret_message)

    error_adapter = VampireSurvivorsAdapter(
        game_exe=tmp_path / "unused-error-game",
        session_dir=tmp_path / "unused-error-session",
        mode="qa",
        bridge_factory=LeakyErrorBridge,
    )
    error_adapter.start(seed=9101, preset="smoke", faults=[])
    error_exit = error_adapter.stop()
    assert error_exit.kind == "error"
    assert error_exit.detail == "RuntimeError"
    assert secret_message not in error_exit.detail

    def adapter_factory(**kwargs: Any) -> VampireSurvivorsAdapter:
        return VampireSurvivorsAdapter(
            **kwargs,
            bridge_factory=LeakyTimeoutBridge,
        )

    monkeypatch.setattr(run_module, "VampireSurvivorsAdapter", adapter_factory)
    run_output = tmp_path / "terminal-exception-run"
    args = run_module.parse_args(
        [
            "--game-exe",
            str(tmp_path / "unused-game"),
            "--project-root",
            str(Path(__file__).resolve().parents[1]),
            "--output",
            str(run_output),
            "--mode",
            "qa",
            "--policy",
            "heuristic",
            "--objective",
            "Exercise safe terminal exception handling.",
            "--max-steps",
            "1",
            "--quiet",
        ]
    )

    run_module.run_session(args)

    run_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(run_output.iterdir())
        if path.is_file()
    )
    assert secret_message not in run_artifacts
    assert "/private/qa/player" not in run_artifacts
    assert "fake-secret" not in run_artifacts
    assert "TimeoutError" in run_artifacts

    spec = exploration.build_track_a_schedule()[0]
    loaded = exploration.BridgeExplorationBackend._load_result(spec, run_output)
    record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-terminal-timeout",
        result=loaded,
        inspection_passes=[artifact(), artifact(), artifact()],
    )
    report = exploration.build_exploration_report([record], metadata={})
    assert report["harness_failures"] == []
    assert report["candidates"][0]["rule"] == "hang"
    assert report["candidates"][0]["priority"] == "P0"

    campaign_root = tmp_path / "checkpoint-campaign"
    campaign_root.mkdir()
    settings = campaign_config(campaign_root)
    build_hash = exploration.hash_path(settings.build)
    campaign_hash = exploration._campaign_hash(settings, build_hash)
    checkpoint = exploration._prepare_campaign_output(settings.output, campaign_hash)
    exploration._run_episode(
        spec=spec,
        output=settings.output,
        backend=ResultBackend(loaded),
        checkpoint=checkpoint,
        campaign_hash=campaign_hash,
    )
    exploration_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(settings.output.rglob("*"))
        if path.is_file()
    )
    public_report = (
        exploration.exploration_report_json(report)
        + exploration.render_exploration_markdown(report)
    )
    assert secret_message not in exploration_artifacts + public_report
    assert "/private/qa/player" not in exploration_artifacts + public_report
    assert "fake-secret" not in exploration_artifacts + public_report


def write_bridge_artifacts(
    output: Path,
    *,
    transitions: list[dict[str, Any]] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    fatal_error: str | None = None,
    write_session_files: bool = True,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "policy": "llm",
        "fatal_error": fatal_error,
        "rule_based_anomalies": anomalies or [],
    }
    (output / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if not write_session_files:
        return
    (output / "steps.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in (transitions or [])),
        encoding="utf-8",
    )
    (output / "verdict.json").write_text(
        json.dumps({"execution_status": "completed"}),
        encoding="utf-8",
    )
    (output / "run.json").write_text(
        json.dumps({"arguments": {"fault": ""}}),
        encoding="utf-8",
    )


def test_terminal_anomaly_uses_last_or_trace_level_public_evidence(tmp_path: Path) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "terminal"
    write_bridge_artifacts(
        output,
        transitions=[transition(0)],
        anomalies=[
            {
                "step": 1,
                "kind": "hang",
                "severity": "critical",
                "evidence": "Player process entered a hang state.",
            }
        ],
    )
    result = exploration.BridgeExplorationBackend._load_result(spec, output)
    record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-terminal",
        result=result,
        inspection_passes=[artifact(), artifact(), artifact()],
    )

    report = exploration.build_exploration_report([record], metadata={})
    terminal = report["candidates"][0]
    assert terminal["rule"] == "hang"
    assert terminal["priority"] == "P0"
    assert terminal["tier"] == "validated-invariant candidate"
    assert terminal["evidence"][0]["evidence_refs"] == ["obs-0000"]
    assert report["surface_counts"]["runtime_oracle"] == 1
    assert report["surface_counts"]["union"] == 0
    markdown = exploration.render_exploration_markdown(report)
    assert "- LLM union: 0" in markdown
    assert "- runtime-oracle-only (별도): 1" in markdown

    empty_output = tmp_path / "empty-terminal"
    write_bridge_artifacts(
        empty_output,
        transitions=[],
        anomalies=[
            {
                "step": 0,
                "kind": "crash",
                "severity": "critical",
                "evidence": "Player crashed before the first observation.",
            }
        ],
    )
    empty = exploration.BridgeExplorationBackend._load_result(spec, empty_output)
    assert empty.trace_evidence_refs == (
        "trace-termination-core-menu-level1-9101",
    )
    assert empty.invariant_validations[0]["evidence_refs"] == list(
        empty.trace_evidence_refs
    )
    empty_record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-empty-terminal",
        result=empty,
        inspection_passes=[None, None, None],
    )
    empty_report = exploration.build_exploration_report([empty_record], metadata={})
    assert empty_report["summary"]["candidate_count"] == 0
    assert "schema_failure" in {
        failure["kind"] for failure in empty_report["harness_failures"]
    }


def test_terminal_anomaly_falls_back_to_the_last_valid_observation(tmp_path: Path) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "terminal-last-valid"
    transition_without_observation = transition(1)
    transition_without_observation["observation"] = {}
    write_bridge_artifacts(
        output,
        transitions=[transition(0), transition_without_observation],
        anomalies=[
            {
                "step": 2,
                "kind": "crash",
                "severity": "critical",
                "evidence": "Player crashed after the last valid observation.",
            }
        ],
    )

    result = exploration.BridgeExplorationBackend._load_result(spec, output)

    assert result.invariant_validations[0]["evidence_refs"] == ["obs-0000"]
    assert not result.trace_evidence_refs


def test_terminal_player_error_is_preserved_as_bridge_failure(
    tmp_path: Path,
) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "terminal-error"
    write_bridge_artifacts(
        output,
        transitions=[transition(0)],
        anomalies=[
            {
                "step": 1,
                "kind": "error",
                "severity": "critical",
                "evidence": "Player process could not be stopped or recovered.",
            }
        ],
    )
    result = exploration.BridgeExplorationBackend._load_result(spec, output)
    assert not result.invariant_validations
    assert result.harness_failures == (
        {"kind": "bridge_failure", "detail": "player termination error"},
    )


def test_runtime_freeze_anomaly_is_preserved_as_hang_candidate(tmp_path: Path) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "runtime-freeze"
    write_bridge_artifacts(
        output,
        transitions=[transition(0)],
        anomalies=[
            {
                "step": 0,
                "kind": "freeze",
                "severity": "critical",
                "evidence": "Frame and simulation time did not advance.",
            }
        ],
    )
    result = exploration.BridgeExplorationBackend._load_result(spec, output)
    record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-runtime-freeze",
        result=result,
        inspection_passes=[artifact(), artifact(), artifact()],
    )

    candidate = exploration.build_exploration_report([record], metadata={})[
        "candidates"
    ][0]
    assert candidate["rule"] == "hang"
    assert candidate["priority"] == "P0"
    assert candidate["statements"] == ["Frame and simulation time did not advance."]


def test_runtime_oracle_validates_matching_llm_candidate_without_inflating_union(
    tmp_path: Path,
) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "runtime-inspector-match"
    write_bridge_artifacts(
        output,
        transitions=[transition(0)],
        anomalies=[
            {
                "step": 1,
                "kind": "hang",
                "severity": "critical",
                "evidence": "Player process entered a hang state.",
            }
        ],
    )
    result = exploration.BridgeExplorationBackend._load_result(spec, output)
    record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-runtime-inspector-match",
        result=result,
        inspection_passes=[artifact(behavior_finding("hang")), artifact(), artifact()],
    )

    report = exploration.build_exploration_report([record], metadata={})

    assert report["surface_counts"] == {
        "planner_only": 0,
        "inspector_only": 1,
        "shared": 0,
        "runtime_oracle": 0,
        "union": 1,
    }
    candidate = report["candidates"][0]
    assert candidate["surface"] == "inspector_only"
    assert candidate["runtime_oracle_supported"] is True
    assert candidate["tier"] == "validated-invariant candidate"


def test_view_state_invariant_matches_only_the_same_numeric_relation(tmp_path: Path) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "view-state"
    write_bridge_artifacts(
        output,
        transitions=[transition(0)],
        anomalies=[
            {
                "step": 0,
                "kind": "view_state_match",
                "severity": "high",
                "evidence": "player_view.health_ratio=1.25 expected=1.0",
            }
        ],
    )
    result = exploration.BridgeExplorationBackend._load_result(spec, output)
    record = exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="opaque-view-state",
        result=result,
        inspection_passes=[
            artifact(
                numeric_finding("player_view.health_ratio"),
                numeric_finding("player_view.exp_ratio"),
            ),
            artifact(),
            artifact(),
        ],
    )

    candidates = {
        candidate["field"]: candidate
        for candidate in exploration.build_exploration_report([record], metadata={})[
            "candidates"
        ]
    }
    assert candidates["player_view.health_ratio"]["tier"] == (
        "validated-invariant candidate"
    )
    assert candidates["player_view.exp_ratio"]["tier"] == (
        "evidence-linked candidate"
    )


def test_report_only_planner_initialization_failure_remains_model_failure(
    tmp_path: Path,
) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "planner-init"
    write_bridge_artifacts(
        output,
        fatal_error=(
            "ValueError: LLM policy requires QA_API_KEY; "
            "provider-secret-body-must-not-be-retained"
        ),
        write_session_files=False,
    )

    result = exploration.BridgeExplorationBackend._load_result(spec, output)

    assert result.execution_status == "model_failure"
    assert result.error == "ValueError"
    assert "provider-secret" not in json.dumps(result.to_dict())


def test_report_only_llm_contract_failure_remains_schema_failure(tmp_path: Path) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "planner-contract"
    write_bridge_artifacts(
        output,
        fatal_error="LLMContractError: private malformed response body",
        write_session_files=False,
    )

    result = exploration.BridgeExplorationBackend._load_result(spec, output)

    assert result.execution_status == "schema_failure"
    assert result.error == "LLMContractError"
    assert "private malformed" not in json.dumps(result.to_dict())


def test_provider_transport_failure_is_not_reclassified_by_private_body_words(
    tmp_path: Path,
) -> None:
    spec = exploration.build_track_a_schedule()[0]
    output = tmp_path / "provider-transport"
    write_bridge_artifacts(
        output,
        fatal_error=(
            "LLMTransportError: provider rejected response schema; private body"
        ),
        write_session_files=False,
    )

    result = exploration.BridgeExplorationBackend._load_result(spec, output)

    assert result.execution_status == "model_failure"
    assert result.error == "LLMTransportError"


def test_markdown_preserves_tier_agreement_evidence_and_json_values() -> None:
    finding = behavior_finding("progress-stalled")
    record = make_record(
        "core-combat-survival",
        9101,
        transitions=[transition(0)],
        passes=[artifact(finding), artifact(finding), artifact()],
        invariant_validations=[
            {
                "category": "behavior",
                "rule": "progress-stalled",
                "field": "",
                "phase": "active_gameplay",
                "event": "",
                "evidence_refs": ["obs-0000"],
            }
        ],
    )
    report = exploration.build_exploration_report([record], metadata={})
    candidate = report["candidates"][0]
    markdown = exploration.render_exploration_markdown(report)

    assert "validated-invariant candidate: 1" in markdown
    assert "evidence-linked candidate: 0" in markdown
    assert "2/3" in markdown
    assert "obs-0000" in markdown
    assert candidate["candidate_id"] in markdown
    assert candidate["priority"] in markdown
    assert ("예" if candidate["reproduced"] else "아니오") in markdown


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
    assert parsed.profile == "full"
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
    assert benchmark.profile == "full"
    assert not hasattr(benchmark, "resume")


def test_cli_parses_poc_profiles_without_requiring_output(tmp_path: Path) -> None:
    explore = cli_module.parse_cli(
        ["explore", "--build", str(tmp_path / "game"), "--profile", "poc"]
    )
    benchmark = cli_module.parse_cli(
        [
            "benchmark-detection",
            "--build",
            str(tmp_path / "game"),
            "--profile",
            "poc",
        ]
    )

    assert explore.output is None
    assert explore.profile == "poc"
    assert benchmark.output is None
    assert benchmark.profile == "poc"


def test_explore_resume_requires_explicit_output(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli_module.parse_cli(
            ["explore", "--build", str(tmp_path / "game"), "--resume"]
        )


def test_timestamped_campaign_output_is_collision_safe(tmp_path: Path) -> None:
    timestamp = datetime(2026, 8, 25, 12, 34, 56, tzinfo=timezone.utc)

    first = cli_module._resolve_campaign_output(
        project_root=tmp_path,
        output=None,
        track="track-a",
        timestamp=timestamp,
    )
    second = cli_module._resolve_campaign_output(
        project_root=tmp_path,
        output=None,
        track="track-a",
        timestamp=timestamp,
    )

    assert first == tmp_path / "QAArtifacts/evaluation/track-a/20260825-123456"
    assert second == tmp_path / "QAArtifacts/evaluation/track-a/20260825-123456-01"
    assert first.is_dir()
    assert second.is_dir()


def test_campaign_output_preserves_explicit_path(tmp_path: Path) -> None:
    explicit = Path("relative/custom-output")

    resolved = cli_module._resolve_campaign_output(
        project_root=tmp_path,
        output=explicit,
        track="track-b",
    )

    assert resolved == explicit
