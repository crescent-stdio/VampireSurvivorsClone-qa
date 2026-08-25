from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from . import detection_campaign as campaign
from . import detection_benchmark as benchmark
from . import cli as cli_module
from .cli import parse_cli


def observation(
    identifier: str,
    *,
    phase: str = "active_gameplay",
    present: bool = True,
) -> dict[str, Any]:
    return {
        "observation_id": identifier,
        "phase": phase,
        "player": {
            "present": present,
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
        "progress": {
            "level_time": 1.0,
            "coins_gained": 0,
            "damage_dealt": 0,
            "damage_taken": 0,
        },
        "menu": {},
        "event_state": {},
    }


def transition(index: int, *, action: str = "observe", phase: str = "active_gameplay"):
    arguments: dict[str, Any] = {}
    if action == "wait":
        arguments = {"duration": 2.5}
    elif action == "select_upgrade":
        arguments = {"index": 1}
    return {
        "step": index,
        "decision": {"tool": "game", "action": action, "arguments": arguments},
        "observation": observation(f"obs-{index:04d}", phase=phase),
    }


class FakeBackend:
    def __init__(self, *, divergence: dict[tuple[str, str], int] | None = None) -> None:
        self.pilots: list[campaign.EpisodeSpec] = []
        self.replays: list[tuple[campaign.EpisodeSpec, str]] = []
        self.autonomous: list[campaign.AutonomousEpisodeSpec] = []
        self.divergence = divergence or {}

    def run_pilot(self, spec: campaign.EpisodeSpec, output_dir: Path) -> campaign.EpisodeResult:
        self.pilots.append(spec)
        return campaign.EpisodeResult(
            transitions=[
                transition(0, action="wait"),
                transition(1, action="select_upgrade"),
            ]
        )

    def run_replay(
        self,
        spec: campaign.EpisodeSpec,
        replay: dict[str, Any],
        output_dir: Path,
    ) -> campaign.EpisodeResult:
        self.replays.append((spec, replay["command_digest"]))
        selected = transition(1, action="select_upgrade")
        selected["decision"].update(
            {
                "scenario_id": "campaign-private-scenario",
                "goal": "campaign-private-goal",
                "fault_id": spec.fault_id,
                "ground_truth": "campaign-private-truth",
                "source_code": "campaign-private-source",
                "qa_observation": "campaign-private-agent-text",
            }
        )
        return campaign.EpisodeResult(
            transitions=[transition(0), selected],
            replay_divergence_index=self.divergence.get((spec.unit_id, spec.variant)),
        )

    def run_autonomous(
        self,
        spec: campaign.AutonomousEpisodeSpec,
        output_dir: Path,
    ) -> campaign.EpisodeResult:
        self.autonomous.append(spec)
        return campaign.EpisodeResult(transitions=[transition(0), transition(1)])


class FakeInspector:
    def __init__(self, *, http_attempts: int = 1, completion_requests: int = 1) -> None:
        self.requests: list[dict[str, Any]] = []
        self.http_attempts = http_attempts
        self.completion_requests = completion_requests

    def inspect(self, system_prompt: str, payload: dict[str, Any]) -> campaign.InspectionResponse:
        self.requests.append(
            {
                "system_prompt": system_prompt,
                "payload": json.loads(json.dumps(payload)),
            }
        )
        return campaign.InspectionResponse(
            raw_response={"schema_version": "qa-inspection/v2", "findings": []},
            usage={
                "request_count": 1,
                "llm_http_attempts": self.http_attempts,
                "llm_completion_requests": self.completion_requests,
                "llm_retries": max(0, self.http_attempts - 1),
                "llm_retry_wait_ms": 0,
            },
            elapsed_seconds=0.01,
        )


def config(tmp_path: Path) -> campaign.BenchmarkCampaignConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    build = tmp_path / "game-build"
    build.write_bytes(b"stable-build")
    return campaign.BenchmarkCampaignConfig(
        build=build,
        project_root=Path(__file__).resolve().parents[1],
        output=tmp_path / "campaign",
        headless=True,
        quiet=True,
    )


def test_action_replay_records_deterministic_commands_and_rejects_tampering() -> None:
    replay = campaign.build_action_replay(
        replay_id="pilot-health-9101",
        scenario_id="easy-health-ratio",
        seed=9101,
        build_hash="a" * 64,
        transitions=[
            transition(0, action="wait", phase="active_gameplay"),
            transition(1, action="select_upgrade", phase="active_gameplay"),
        ],
        target_command_index=1,
    )

    assert replay["schema_version"] == "qa-action-replay/v1"
    assert replay["seed"] == 9101
    assert replay["scenario_id"] == "easy-health-ratio"
    assert replay["build_hash"] == "a" * 64
    assert replay["bootstrap_command"] == {"action": "observe", "arguments": {}}
    assert replay["commands"] == [
        {
            "sequence": 0,
            "action": "wait",
            "arguments": {"duration": 2.5},
            "duration_seconds": 2.5,
            "semantic_selection": None,
            "expected_phase_before": None,
            "expected_phase_after": "active_gameplay",
        },
        {
            "sequence": 1,
            "action": "select_upgrade",
            "arguments": {"index": 1},
            "duration_seconds": 0.0,
            "semantic_selection": {"kind": "upgrade", "index": 1},
            "expected_phase_before": "active_gameplay",
            "expected_phase_after": "active_gameplay",
        },
    ]
    campaign.validate_action_replay(replay, expected_build_hash="a" * 64)

    target_tampered = json.loads(json.dumps(replay))
    target_tampered["target_command_index"] = 0
    with pytest.raises(campaign.CampaignContractError, match="replay artifact digest"):
        campaign.validate_action_replay(target_tampered, expected_build_hash="a" * 64)

    replay["commands"][0]["duration_seconds"] = 9.0
    with pytest.raises(campaign.CampaignContractError, match="command digest"):
        campaign.validate_action_replay(replay, expected_build_hash="a" * 64)


def test_fixed_track_b_schedule_has_symmetric_official_and_autonomous_pairs(tmp_path: Path) -> None:
    settings = config(tmp_path)
    bindings = campaign.load_fault_bindings(settings.project_root)
    schedule = campaign.build_track_b_schedule(bindings)

    assert len(bindings) == 11
    assert len(schedule.pilots) == 33
    assert len(schedule.official_pairs) == 33
    assert len(schedule.autonomous_pairs) == 11
    assert {item.seed for item in schedule.pilots} == {9101, 9102, 9103}
    assert {pair.clean.seed for pair in schedule.autonomous_pairs} == {9101}
    for pair in (*schedule.official_pairs, *schedule.autonomous_pairs):
        assert pair.clean.fault_id == pair.fault.fault_id
        assert pair.clean.seed == pair.fault.seed
        assert pair.clean.variant == "clean"
        assert pair.fault.variant == "fault"
    assert campaign._planned_inspection_call_counts(schedule) == {
        "track_a": 189,
        "track_b": 504,
        "cross_track": 693,
    }


def test_poc_track_b_schedule_has_exact_faults_and_fifteen_launches(tmp_path: Path) -> None:
    settings = config(tmp_path)
    bindings = campaign.load_fault_bindings(settings.project_root)
    schedule = campaign.build_track_b_schedule(bindings, "poc")
    selected_faults = {
        "health_bar_desync",
        "item_effect_not_applied",
        "experience_display_drift",
    }

    assert len(schedule.pilots) == 3
    assert len(schedule.official_pairs) == 3
    assert len(schedule.autonomous_pairs) == 3
    assert (
        len(schedule.pilots)
        + len(schedule.official_pairs) * 2
        + len(schedule.autonomous_pairs) * 2
        == 15
    )
    assert {spec.fault_id for spec in schedule.pilots} == selected_faults
    assert {spec.seed for spec in schedule.pilots} == {9101}
    assert {pair.clean.fault_id for pair in schedule.official_pairs} == selected_faults
    assert {pair.clean.fault_id for pair in schedule.autonomous_pairs} == selected_faults


def test_track_b_profile_changes_campaign_identity(tmp_path: Path) -> None:
    full = config(tmp_path)
    poc = replace(full, profile="poc")
    build_hash = campaign.hash_path(full.build)

    assert campaign._campaign_hash(full, build_hash) != campaign._campaign_hash(
        poc, build_hash
    )


def test_campaign_runs_full_schedule_blindly_and_excludes_pilots_from_scores(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    inspector = FakeInspector()
    result = campaign.run_detection_campaign(config(tmp_path), backend=backend, inspector=inspector)

    assert len(backend.pilots) == 33
    assert len(backend.replays) == 66
    assert len(backend.autonomous) == 22
    assert len(inspector.requests) == 88 * 3
    assert all(spec.driver == "llm" for spec in backend.autonomous)
    assert all(spec.model == "gpt-4o-mini" for spec in backend.autonomous)
    assert all(spec.goal == campaign.NEUTRAL_REACHABILITY_GOAL for spec in backend.autonomous)
    assert all(spec.source_tools_enabled is False for spec in backend.autonomous)

    replay_digests: dict[str, set[str]] = {}
    for spec, digest in backend.replays:
        replay_digests.setdefault(spec.pair_id, set()).add(digest)
    assert len(replay_digests) == 33
    assert all(len(digests) == 1 for digests in replay_digests.values())

    manifest = json.loads((config(tmp_path).output / "campaign-manifest.json").read_text())
    assert manifest["schema_version"] == "qa-campaign-manifest/v1"
    assert manifest["counts"] == {
        "pilots": 33,
        "official_replay_traces": 66,
        "autonomous_traces": 22,
        "scored_pairs": 44,
        "logical_inspection_calls": 264,
        "http_attempts": 264,
    }
    assert len(manifest["hashes"]["rubric"]) == 64
    assert len(manifest["hashes"]["inspection_request_contract"]) == 64
    assert len(manifest["hashes"]["scoring_contract"]) == 64
    assert (
        manifest["inspection_request_contract_version"]
        == campaign.INSPECTION_REQUEST_CONTRACT_VERSION
    )
    assert manifest["scoring_contract_version"] == campaign.SCORING_CONTRACT_VERSION
    assert manifest["limits"]["planned_track_a"] == 189
    assert manifest["limits"]["planned_track_b"] == 504
    assert manifest["limits"]["planned_cross_track_max"] == 693
    assert all(len(trace["opaque_trace_id"]) == 24 for trace in manifest["traces"])
    assert len(result.pairs) == 44
    assert all("pilot" not in pair.pair_id for pair in result.pairs)
    assert (config(tmp_path).output / "detection-benchmark.json").exists()
    public_report = json.loads(
        (config(tmp_path).output / "detection-benchmark.json").read_text()
    )
    assert (
        public_report["metadata"]["scoring_contract_hash"]
        == manifest["hashes"]["scoring_contract"]
    )
    assert (
        public_report["metadata"]["scoring_contract_version"]
        == campaign.SCORING_CONTRACT_VERSION
    )
    assert (config(tmp_path).output / "detection-benchmark.ko.md").read_text().startswith(
        "# 주입 결함 탐지 벤치마크"
    )

    forbidden = (
        "scenario_id",
        "scenario",
        "goal",
        "fault_id",
        "ground_truth",
        "oracle",
        "source_code",
        "source_tools",
        "health_ratio_out_of_range",
    )
    serialized_requests = json.dumps(
        [request["payload"] for request in inspector.requests], ensure_ascii=False
    ).lower()
    for marker in forbidden:
        assert marker not in serialized_requests
    assert "action_context" in serialized_requests
    assert "select_upgrade" in serialized_requests
    assert "campaign-private" not in serialized_requests


def test_official_bridge_ids_remain_blind_and_support_a_numeric_true_positive(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    build_hash = campaign.hash_path(settings.build)
    bindings = campaign.load_fault_bindings(settings.project_root)
    binding = next(
        item for item in bindings if item.fault_id == "health_ratio_out_of_range"
    )
    pair = next(
        item
        for item in campaign.build_track_b_schedule(bindings).official_pairs
        if item.clean.fault_id == binding.fault_id
    )
    clean_run_id = campaign.official_bridge_run_id(build_hash, pair.clean)
    fault_run_id = campaign.official_bridge_run_id(build_hash, pair.fault)

    forbidden = {
        pair.clean.fault_id,
        pair.clean.scenario_id,
        pair.clean.variant,
        pair.fault.variant,
        "goal",
    }
    assert clean_run_id != fault_run_id
    assert all(token not in clean_run_id for token in forbidden)
    assert all(token not in fault_run_id for token in forbidden)

    def ratio_transition(run_id: str, ratio: float) -> dict[str, Any]:
        observation_id = f"observation:{run_id}:00000001"
        event_id = f"event:{run_id}:00000001"
        return {
            "step": 0,
            "decision": {"tool": "game", "action": "observe", "arguments": {}},
            "observation": {
                **observation(observation_id),
                "player": {
                    "present": True,
                    "health": 96.0,
                    "max_health": 100.0,
                    "health_ratio": ratio,
                    "exp": 0.0,
                    "next_level_exp": 10.0,
                    "exp_ratio": 0.0,
                    "level": 1,
                },
                "event_state": {"event_id": event_id, "type": "tick"},
            },
        }

    clean_result = campaign.EpisodeResult(
        transitions=[ratio_transition(clean_run_id, 0.96)]
    )
    fault_result = campaign.EpisodeResult(
        transitions=[ratio_transition(fault_run_id, 1.25)]
    )

    class NumericInspector(FakeInspector):
        def inspect(self, system_prompt: str, payload: dict[str, Any]):
            self.requests.append(
                {"system_prompt": system_prompt, "payload": json.loads(json.dumps(payload))}
            )
            row = payload["observations"][0]
            player = row["player"]
            expected = player["health"] / player["max_health"]
            findings = []
            if player["health_ratio"] != expected:
                findings.append(
                    {
                        "kind": "numeric",
                        "field": "player.health_ratio",
                        "comparison": "!=",
                        "expected_value": expected,
                        "observed_value": player["health_ratio"],
                        "statement": "The reported health ratio differs from health / max_health.",
                        "evidence_refs": [row["observation_id"]],
                    }
                )
            return campaign.InspectionResponse(
                raw_response={
                    "schema_version": "qa-inspection/v2",
                    "findings": findings,
                },
                usage={"llm_http_attempts": 1, "llm_completion_requests": 1},
            )

    inspector = NumericInspector()
    campaign_hash = campaign._campaign_hash(settings, build_hash)
    checkpoint = campaign.CheckpointStore(
        settings.output / "checkpoint.json", campaign_hash
    )
    score = campaign._score_episode_pair(
        pair=pair,
        clean_result=clean_result,
        fault_result=fault_result,
        binding=binding,
        config=settings,
        inspector=inspector,
        checkpoint=checkpoint,
        budget=campaign.InspectionCallBudget(),
        campaign_hash=campaign_hash,
    )

    assert score.clean.status == "TN"
    assert score.fault.status == "TP"
    assert score.fault.target_findings[0]["evidence_refs"] == [
        f"observation:{fault_run_id}:00000001"
    ]
    serialized_requests = json.dumps(
        [request["payload"] for request in inspector.requests],
        ensure_ascii=False,
    )
    assert f"observation:{clean_run_id}:00000001" in serialized_requests
    assert f"event:{fault_run_id}:00000001" in serialized_requests
    assert all(token not in serialized_requests for token in forbidden)


def test_divergence_before_target_is_not_reached_but_post_target_is_preserved() -> None:
    replay = campaign.build_action_replay(
        replay_id="pilot",
        scenario_id="scenario",
        seed=9101,
        build_hash="b" * 64,
        transitions=[transition(0), transition(1), transition(2)],
        target_command_index=1,
    )
    before = campaign.apply_replay_divergence(
        campaign.EpisodeResult(
            transitions=[transition(0)],
            replay_divergence_index=1,
            replay_divergence_stage="before_command",
        ),
        replay,
    )
    after = campaign.apply_replay_divergence(
        campaign.EpisodeResult(
            transitions=[transition(0), transition(1)],
            replay_divergence_index=1,
            replay_divergence_stage="after_command",
        ),
        replay,
    )

    assert before.coverage_override == "not_reached"
    assert before.oracle_override == "not_evaluated"
    assert before.divergence_evidence == {
        "classification": "pre_target",
        "command_index": 1,
        "comparison_stage": "before_command",
        "target_command_index": 1,
    }
    assert after.coverage_override is None
    assert after.oracle_override is None
    assert after.divergence_evidence == {
        "classification": "post_target",
        "command_index": 1,
        "comparison_stage": "after_command",
        "target_command_index": 1,
    }


def test_v4_replay_target_indexes_begin_only_at_the_declared_goal_boundary() -> None:
    project_root = Path(__file__).resolve().parents[1]
    bindings = campaign.load_fault_bindings(project_root)
    view_binding = next(item for item in bindings if item.fault_id == "health_bar_desync")
    exp_binding = next(
        item for item in bindings if item.fault_id == "experience_display_drift"
    )

    player_only = transition(0)
    player_only["observation"].pop("player_view")
    player_and_view = transition(1)
    assert campaign._target_command_index(
        view_binding, project_root, [player_only]
    ) is None
    assert campaign._target_command_index(
        view_binding, project_root, [player_only, player_and_view]
    ) == 1

    exp_rows = [transition(index) for index in range(3)]
    for level, row in enumerate(exp_rows, start=1):
        row["observation"]["player"]["level"] = level
    assert campaign._target_command_index(
        exp_binding, project_root, exp_rows[:2]
    ) is None
    assert campaign._target_command_index(exp_binding, project_root, exp_rows) == 2

    replay = campaign.build_action_replay(
        replay_id="opaque-pilot",
        scenario_id=exp_binding.scenario_id,
        seed=9101,
        build_hash="c" * 64,
        transitions=exp_rows,
        target_command_index=2,
    )
    before = campaign.apply_replay_divergence(
        campaign.EpisodeResult(
            transitions=exp_rows[:2],
            replay_divergence_index=2,
            replay_divergence_stage="before_command",
        ),
        replay,
    )
    after = campaign.apply_replay_divergence(
        campaign.EpisodeResult(
            transitions=exp_rows,
            replay_divergence_index=2,
            replay_divergence_stage="after_command",
        ),
        replay,
    )
    assert before.coverage_override == "not_reached"
    assert after.coverage_override is None


def test_inspection_logical_call_budget_counts_retries_as_http_attempts(tmp_path: Path) -> None:
    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    budget = campaign.InspectionCallBudget(cap=700)
    inspector = FakeInspector(http_attempts=3)

    artifact = campaign.inspect_trace_pass(
        trace_id="opaque-1",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=tmp_path / "inspection",
        inspector=inspector,
        checkpoint=checkpoint,
        budget=budget,
        input_hash="input-hash",
    )

    assert artifact == {"schema_version": "qa-inspection/v2", "findings": []}
    assert budget.logical_calls == 1
    assert budget.http_attempts == 3
    assert len(inspector.requests) == 1

    truncated = FakeInspector(http_attempts=4, completion_requests=2)
    campaign.inspect_trace_pass(
        trace_id="opaque-truncation",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=tmp_path / "inspection-truncation",
        inspector=truncated,
        checkpoint=checkpoint,
        budget=budget,
        input_hash="truncation-input",
    )
    assert budget.logical_calls == 2
    assert budget.http_attempts == 7

    over_retrying = FakeInspector(http_attempts=4)
    with pytest.raises(campaign.CampaignContractError, match="three HTTP attempts"):
        campaign.inspect_trace_pass(
            trace_id="opaque-2",
            pass_index=1,
            transitions=[transition(0)],
            output_dir=tmp_path / "inspection-2",
            inspector=over_retrying,
            checkpoint=checkpoint,
            budget=budget,
            input_hash="different-input",
        )
    assert budget.logical_calls == 3


def test_campaign_contract_errors_are_not_downgraded_to_inspection_errors(tmp_path: Path) -> None:
    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")

    with pytest.raises(campaign.CampaignContractError, match="call cap"):
        campaign._inspect_episode(
            opaque_trace_id="opaque-cap",
            result=campaign.EpisodeResult(transitions=[transition(0)]),
            output=tmp_path,
            inspector=FakeInspector(),
            checkpoint=checkpoint,
            budget=campaign.InspectionCallBudget(cap=0),
            campaign_hash="campaign-hash",
        )


def test_checkpoint_and_episode_failure_artifacts_store_only_error_types(
    tmp_path: Path,
) -> None:
    secret = "token=fake-secret at /private/provider"
    checkpoint_path = tmp_path / "checkpoint.json"
    episode_path = tmp_path / "episode-result.json"
    checkpoint = campaign.CheckpointStore(checkpoint_path, "campaign-hash")

    with pytest.raises(campaign.CampaignExecutionError):
        campaign._run_episode_unit(
            checkpoint=checkpoint,
            unit_id="official/opaque/fault",
            input_hash="input-hash",
            path=episode_path,
            execute=lambda: campaign.EpisodeResult(
                transitions=[],
                execution_status="infrastructure_error",
                error=f"RuntimeError: {secret}",
            ),
        )

    episode = json.loads(episode_path.read_text())
    checkpoint_payload = json.loads(checkpoint_path.read_text())
    serialized = json.dumps([episode, checkpoint_payload])
    assert episode["error"] == "RuntimeError"
    assert checkpoint_payload["units"]["official/opaque/fault"]["error"] == (
        "CampaignExecutionError"
    )
    assert "fake-secret" not in serialized
    assert "/private/provider" not in serialized


def test_public_failure_artifacts_reject_unclassified_alphanumeric_messages(
    tmp_path: Path,
) -> None:
    secret = "fakeSecretCredential123"
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = campaign.CheckpointStore(checkpoint_path, "campaign-hash")

    episode = campaign.EpisodeResult(
        transitions=[],
        execution_status="infrastructure_error",
        error=secret,
    ).to_dict()
    checkpoint.mark_failed("unit", "input-hash", secret)
    checkpoint_payload = json.loads(checkpoint_path.read_text())

    serialized = json.dumps([episode, checkpoint_payload])
    assert episode["error"] == "Exception"
    assert checkpoint_payload["units"]["unit"]["error"] == "Exception"
    assert secret not in serialized


@pytest.mark.parametrize("failure_surface", ["audit", "checkpoint"])
def test_filesystem_write_failures_abort_track_b_with_an_incomplete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_surface: str,
) -> None:
    settings = config(tmp_path / failure_surface)
    if failure_surface == "audit":
        original_write = campaign._atomic_write_json

        def fail_audit(path: Path, value: dict[str, Any]) -> None:
            if path.name.endswith(".audit.json"):
                raise OSError("audit write failed at /private/path token=fake-secret")
            original_write(path, value)

        monkeypatch.setattr(campaign, "_atomic_write_json", fail_audit)
    else:
        def fail_checkpoint(self: campaign.CheckpointStore) -> None:
            raise OSError("checkpoint write failed at /private/path token=fake-secret")

        monkeypatch.setattr(campaign.CheckpointStore, "record_logical_call", fail_checkpoint)

    with pytest.raises(campaign.CampaignExecutionError):
        campaign.run_detection_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"]["error_type"] == "OSError"
    assert not (settings.output / "detection-benchmark.json").exists()
    assert "/private/path" not in manifest_text
    assert "fake-secret" not in manifest_text


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        {"schema_version": "qa-inspection/v2"},
        {
            "schema_version": "qa-inspection/v2",
            "findings": [{"kind": "numeric", "field": "player.health"}],
        },
    ],
)
def test_malformed_campaign_inspections_are_failed_not_empty_negative_votes(
    tmp_path: Path,
    malformed: Any,
) -> None:
    class MalformedInspector:
        def inspect(self, system_prompt: str, payload: dict[str, Any]):
            return campaign.InspectionResponse(
                raw_response=malformed,
                usage={"llm_http_attempts": 1, "llm_completion_requests": 1},
            )

    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    passes = campaign._inspect_episode(
        opaque_trace_id="opaque-malformed",
        result=campaign.EpisodeResult(transitions=[transition(0)]),
        output=tmp_path,
        inspector=MalformedInspector(),
        checkpoint=checkpoint,
        budget=campaign.InspectionCallBudget(),
        campaign_hash="campaign-hash",
    )

    assert passes == [None, None, None]
    chunk_units = [
        value
        for key, value in checkpoint.units.items()
        if key.endswith("inspection-chunk-000000-000000")
    ]
    assert len(chunk_units) == 3
    assert {unit["status"] for unit in chunk_units} == {"failed"}
    for pass_index in (1, 2, 3):
        audit = json.loads(
            (
                tmp_path
                / "inspections"
                / "opaque-malformed"
                / f"pass-{pass_index}"
                / "inspection-chunk-000000-000000.audit.json"
            ).read_text()
        )
        assert audit["status"] == "error"
        assert audit["raw_response"] == malformed
        assert audit["usage"]["llm_http_attempts"] == 1
    scored = benchmark.score_trace(
        benchmark.TraceEvaluation.fault(
            "opaque-malformed",
            "health_ratio_out_of_range",
            [
                {
                    "observation": {
                        "observation_id": "obs-malformed",
                        "player": {
                            "present": True,
                            "health": 96.0,
                            "max_health": 100.0,
                            "health_ratio": 1.25,
                        },
                    }
                }
            ],
            passes,
        )
    )
    assert scored.status == "INSPECTION_ERROR"


def test_failed_inspection_chunks_audit_attempts_without_credentials(tmp_path: Path) -> None:
    class FailingInspector:
        def inspect(self, system_prompt: str, payload: dict[str, Any]):
            raise campaign.InspectionCallError(
                "provider failed secret-token",
                usage={"llm_http_attempts": 3, "llm_retries": 2},
                elapsed_seconds=0.25,
            )

    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    budget = campaign.InspectionCallBudget()
    with pytest.raises(campaign.InspectionCallError):
        campaign.inspect_trace_pass(
            trace_id="opaque-failure",
            pass_index=1,
            transitions=[transition(0)],
            output_dir=tmp_path / "inspection",
            inspector=FailingInspector(),
            checkpoint=checkpoint,
            budget=budget,
            input_hash="input-hash",
        )

    audit_path = (
        tmp_path
        / "inspection"
        / "pass-1"
        / "inspection-chunk-000000-000000.audit.json"
    )
    audit = json.loads(audit_path.read_text())
    assert audit["status"] == "error"
    assert audit["usage"]["llm_http_attempts"] == 3
    assert "secret-token" not in json.dumps(audit)
    assert budget.logical_calls == 1
    assert budget.http_attempts == 3


def test_preparse_model_failures_audit_distinct_cause_types_without_stale_state(
    tmp_path: Path,
) -> None:
    model_failures = (
        ("not JSON", "JSONDecodeError"),
        ('["not", "an", "object"]', "ValueError"),
    )
    envelopes = iter(
        json.dumps(
            {
                "id": "provider-metadata-must-not-be-audited",
                "choices": [
                    {
                        "message": {"content": model_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            }
        ).encode()
        for model_content, _cause_type in model_failures
    )

    class ProviderResponse:
        def __init__(self, envelope: bytes) -> None:
            self.envelope = envelope

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.envelope

    adapter = campaign.LLMInspectorAdapter.__new__(campaign.LLMInspectorAdapter)
    adapter.planner = campaign.LLMPlanner(
        "qa",
        campaign.INSPECTOR_MODEL,
        campaign.TestCharter(objective=campaign.NEUTRAL_REACHABILITY_GOAL),
        5.0,
        "https://example.invalid/v1/chat/completions",
        api_key="request-credential-must-not-be-audited",
        urlopen=lambda *_args, **_kwargs: ProviderResponse(next(envelopes)),
        max_attempts=1,
    )
    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    budget = campaign.InspectionCallBudget()

    for index, (model_content, expected_cause_type) in enumerate(model_failures):
        output_dir = tmp_path / f"inspection-{index}"
        with pytest.raises(campaign.InspectionCallError) as raised:
            campaign.inspect_trace_pass(
                trace_id=f"opaque-preparse-{index}",
                pass_index=1,
                transitions=[transition(0)],
                output_dir=output_dir,
                inspector=adapter,
                checkpoint=checkpoint,
                budget=budget,
                input_hash=f"input-hash-{index}",
            )

        assert raised.value.cause_type == expected_cause_type
        audit = json.loads(
            (
                output_dir
                / "pass-1"
                / "inspection-chunk-000000-000000.audit.json"
            ).read_text()
        )
        assert audit["status"] == "error"
        assert audit["error_type"] == "InspectionCallError"
        assert audit["cause_type"] == expected_cause_type
        assert audit["raw_response"] == model_content
        assert audit["usage"]["prompt_tokens"] == 4
        serialized = json.dumps(audit)
        assert "provider-metadata-must-not-be-audited" not in serialized
        assert "request-credential-must-not-be-audited" not in serialized


def test_complete_checkpoints_resume_without_repeating_external_work(tmp_path: Path) -> None:
    settings = config(tmp_path)
    first_backend = FakeBackend()
    first_inspector = FakeInspector()
    campaign.run_detection_campaign(settings, backend=first_backend, inspector=first_inspector)

    resumed_backend = FakeBackend()
    resumed_inspector = FakeInspector()
    resumed = campaign.run_detection_campaign(
        settings,
        backend=resumed_backend,
        inspector=resumed_inspector,
    )

    assert resumed.resumed is True
    assert resumed_backend.pilots == []
    assert resumed_backend.replays == []
    assert resumed_backend.autonomous == []
    assert resumed_inspector.requests == []


def test_autonomous_steering_prompt_identity_rejects_changed_resume_only_for_llm_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )

    unchanged_backend = FakeBackend()
    unchanged = campaign.run_detection_campaign(
        settings,
        backend=unchanged_backend,
        inspector=FakeInspector(),
    )
    assert unchanged.resumed is True
    assert unchanged_backend.pilots == []
    assert unchanged_backend.replays == []
    assert unchanged_backend.autonomous == []
    manifest = json.loads(unchanged.manifest_path.read_text())
    assert manifest["hashes"]["steering_prompt"] == campaign._steering_prompt_digest()
    assert (
        manifest["steering_prompt_version"]
        == campaign.STEERING_PROMPT_TEMPLATE_VERSION
    )
    assert "planning_system_prompt" not in unchanged.manifest_path.read_text()

    bindings = campaign.load_fault_bindings(settings.project_root)
    schedule = campaign.build_track_b_schedule(bindings)
    official = schedule.official_pairs[0].clean
    autonomous = schedule.autonomous_pairs[0].clean
    official_hash = campaign._unit_hash(
        campaign_hash="fixed-campaign",
        spec=official,
        replay_digest="fixed-replay",
    )
    autonomous_hash = campaign._unit_hash(
        campaign_hash="fixed-campaign",
        spec=autonomous,
    )

    with monkeypatch.context() as horizon_patch:
        horizon_patch.setattr(campaign, "STEERING_PLAN_HORIZON_SECONDS", 7.5)
        assert campaign._unit_hash(
            campaign_hash="fixed-campaign",
            spec=official,
            replay_digest="fixed-replay",
        ) == official_hash
        assert campaign._unit_hash(
            campaign_hash="fixed-campaign",
            spec=autonomous,
        ) != autonomous_hash

    monkeypatch.setattr(
        campaign,
        "_steering_prompt_digest",
        lambda: "changed-autonomous-steering-prompt-digest",
        raising=False,
    )

    assert campaign._unit_hash(
        campaign_hash="fixed-campaign",
        spec=official,
        replay_digest="fixed-replay",
    ) == official_hash
    assert campaign._unit_hash(
        campaign_hash="fixed-campaign",
        spec=autonomous,
    ) != autonomous_hash
    with pytest.raises(campaign.CampaignContractError, match="campaign hash mismatch"):
        campaign.run_detection_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )


def test_regenerated_episode_invalidates_only_its_dependent_inspections(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    regenerated_unit = "official/health_ratio_out_of_range/9101/clean"
    regenerated_trace = next(
        item["opaque_trace_id"]
        for item in manifest["traces"]
        if item["unit_id"] == regenerated_unit
    )
    untouched_trace = next(
        item["opaque_trace_id"]
        for item in manifest["traces"]
        if item["unit_id"] == "official/health_ratio_out_of_range/9101/fault"
    )
    checkpoint_path = settings.output / "checkpoint.json"
    before_checkpoint = json.loads(checkpoint_path.read_text())
    untouched_before = {
        unit_id: value
        for unit_id, value in before_checkpoint["units"].items()
        if unit_id.startswith(f"inspection/{untouched_trace}/")
    }
    dependent_before = {
        unit_id: value
        for unit_id, value in before_checkpoint["units"].items()
        if unit_id.startswith(f"inspection/{regenerated_trace}/")
    }
    assert len(dependent_before) == 6

    episode_path = settings.output / "traces" / regenerated_unit / "episode-result.json"
    episode_path.write_text('{"transitions": []}\n', encoding="utf-8")

    class RegeneratedTraceBackend(FakeBackend):
        def run_replay(self, spec, replay, output_dir):
            result = super().run_replay(spec, replay, output_dir)
            rows = json.loads(json.dumps(result.transitions))
            rows[0]["command_id"] = "new-command-after-bridge-restart"
            rows[0]["observation"]["observation_id"] = "new-observation-after-bridge-restart"
            return replace(result, transitions=rows)

    resumed_backend = RegeneratedTraceBackend()
    resumed_inspector = FakeInspector()
    result = campaign.run_detection_campaign(
        settings,
        backend=resumed_backend,
        inspector=resumed_inspector,
    )

    assert result.resumed is True
    assert [spec.unit_id for spec, _digest in resumed_backend.replays] == [
        regenerated_unit
    ]
    assert len(resumed_inspector.requests) == 3
    assert all(
        "new-observation-after-bridge-restart" in json.dumps(request)
        for request in resumed_inspector.requests
    )
    after_checkpoint = json.loads(checkpoint_path.read_text())
    dependent_after = {
        unit_id: value
        for unit_id, value in after_checkpoint["units"].items()
        if unit_id.startswith(f"inspection/{regenerated_trace}/")
    }
    assert set(dependent_after) == set(dependent_before)
    assert all(
        dependent_after[unit_id]["input_hash"]
        != dependent_before[unit_id]["input_hash"]
        for unit_id in dependent_before
    )
    assert {
        unit_id: value
        for unit_id, value in after_checkpoint["units"].items()
        if unit_id.startswith(f"inspection/{untouched_trace}/")
    } == untouched_before


def test_effective_api_url_environment_change_rejects_exact_resume_without_leaking_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    first_url = "https://user:credential-one@example.invalid/v1/chat?token=secret-one"
    second_url = "https://user:credential-two@example.invalid/v2/chat?token=secret-two"
    monkeypatch.setenv("QA_API_URL", first_url)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert len(manifest["hashes"]["api_endpoint"]) == 64
    assert "credential-one" not in manifest_text
    assert "secret-one" not in manifest_text

    monkeypatch.setenv("QA_API_URL", second_url)
    with pytest.raises(campaign.CampaignContractError, match="campaign hash mismatch"):
        campaign.run_detection_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )


def test_checkpoint_hashes_prevent_stale_episode_pass_and_chunk_reuse(tmp_path: Path) -> None:
    checkpoint = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    episode_path = tmp_path / "episode.json"
    episode_calls = 0

    def execute_episode() -> campaign.EpisodeResult:
        nonlocal episode_calls
        episode_calls += 1
        return campaign.EpisodeResult(transitions=[transition(episode_calls)])

    campaign._run_episode_unit(
        checkpoint=checkpoint,
        unit_id="episode/example",
        input_hash="episode-input",
        path=episode_path,
        execute=execute_episode,
    )
    episode_path.write_text('{"transitions": []}\n', encoding="utf-8")
    restored, resumed = campaign._run_episode_unit(
        checkpoint=checkpoint,
        unit_id="episode/example",
        input_hash="episode-input",
        path=episode_path,
        execute=execute_episode,
    )
    assert resumed is False
    assert episode_calls == 2
    assert restored.transitions[0]["step"] == 2

    inspector = FakeInspector()
    budget = campaign.InspectionCallBudget()
    pass_dir = tmp_path / "inspection"
    campaign.inspect_trace_pass(
        trace_id="opaque-integrity",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=pass_dir,
        inspector=inspector,
        checkpoint=checkpoint,
        budget=budget,
        input_hash="inspection-input",
    )
    pass_path = pass_dir / "pass-1" / "inspection.json"
    chunk_path = pass_dir / "pass-1" / "inspection-chunk-000000-000000.audit.json"

    pass_path.write_text("{not-json}\n", encoding="utf-8")
    restored_pass = campaign.inspect_trace_pass(
        trace_id="opaque-integrity",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=pass_dir,
        inspector=inspector,
        checkpoint=checkpoint,
        budget=budget,
        input_hash="inspection-input",
    )
    assert restored_pass == {"schema_version": "qa-inspection/v2", "findings": []}
    assert len(inspector.requests) == 1

    chunk_path.write_text("{}\n", encoding="utf-8")
    campaign.inspect_trace_pass(
        trace_id="opaque-integrity",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=pass_dir,
        inspector=inspector,
        checkpoint=checkpoint,
        budget=budget,
        input_hash="inspection-input",
    )
    assert len(inspector.requests) == 2

    checkpoint_payload = json.loads(checkpoint.path.read_text())
    chunk_unit = (
        "inspection/opaque-integrity/pass-1/inspection-chunk-000000-000000"
    )
    checkpoint_payload["units"][chunk_unit]["status"] = "failed"
    checkpoint.path.write_text(json.dumps(checkpoint_payload), encoding="utf-8")
    reloaded = campaign.CheckpointStore(checkpoint.path, "campaign-hash")
    campaign.inspect_trace_pass(
        trace_id="opaque-integrity",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=pass_dir,
        inspector=inspector,
        checkpoint=reloaded,
        budget=budget,
        input_hash="inspection-input",
    )
    assert len(inspector.requests) == 3


def test_failed_or_incomplete_checkpoint_units_are_rerun(tmp_path: Path) -> None:
    store = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    store.mark_failed("pilot/example", "unit-hash", "boom")
    assert store.reusable("pilot/example", "unit-hash", [tmp_path / "pilot.json"]) is False
    store.mark_started("pilot/example", "unit-hash")
    assert store.reusable("pilot/example", "unit-hash", [tmp_path / "pilot.json"]) is False

    result_path = tmp_path / "failed-episode.json"
    with pytest.raises(campaign.CampaignExecutionError, match="episode execution failed"):
        campaign._run_episode_unit(
            checkpoint=store,
            unit_id="replay/failed",
            input_hash="replay-hash",
            path=result_path,
            execute=lambda: campaign.EpisodeResult(
                transitions=[], execution_status="infrastructure_error", error="bridge stopped"
            ),
        )
    assert store.reusable("replay/failed", "replay-hash", [result_path]) is False


def test_failed_pilot_stops_campaign_without_building_or_scoring_replay(tmp_path: Path) -> None:
    class FailedPilotBackend(FakeBackend):
        def run_pilot(
            self, spec: campaign.EpisodeSpec, output_dir: Path
        ) -> campaign.EpisodeResult:
            self.pilots.append(spec)
            return campaign.EpisodeResult(
                transitions=[],
                execution_status="infrastructure_error",
                error="pilot bridge stopped",
            )

    settings = config(tmp_path)
    backend = FailedPilotBackend()
    with pytest.raises(campaign.CampaignExecutionError, match="pilot execution failed"):
        campaign.run_detection_campaign(
            settings,
            backend=backend,
            inspector=FakeInspector(),
        )

    assert len(backend.pilots) == 1
    assert backend.replays == []
    assert not list(settings.output.rglob("action-replay.json"))
    assert not (settings.output / "detection-benchmark.json").exists()
    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert manifest["failure"]["status"] == "failed"


def test_episode_and_evaluator_exceptions_fail_campaign_without_score_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EpisodeExceptionBackend(FakeBackend):
        def run_replay(self, spec, replay, output_dir):
            raise RuntimeError("episode adapter exploded")

    episode_settings = config(tmp_path / "episode")
    with pytest.raises(campaign.CampaignExecutionError, match="campaign execution failed"):
        campaign.run_detection_campaign(
            episode_settings,
            backend=EpisodeExceptionBackend(),
            inspector=FakeInspector(),
        )
    episode_manifest = json.loads(
        (episode_settings.output / "campaign-manifest.json").read_text()
    )
    assert episode_manifest["status"] == "incomplete"
    assert not (episode_settings.output / "detection-benchmark.json").exists()

    evaluator_settings = config(tmp_path / "evaluator")
    evaluator_backend = FakeBackend()
    original_evaluate = campaign._evaluate_binding

    def fail_after_replay(binding, project_root, transitions):
        if evaluator_backend.replays:
            raise RuntimeError("oracle evaluator exploded")
        return original_evaluate(binding, project_root, transitions)

    monkeypatch.setattr(campaign, "_evaluate_binding", fail_after_replay)
    evaluator_inspector = FakeInspector()
    with pytest.raises(campaign.CampaignExecutionError, match="campaign execution failed"):
        campaign.run_detection_campaign(
            evaluator_settings,
            backend=evaluator_backend,
            inspector=evaluator_inspector,
        )
    evaluator_manifest = json.loads(
        (evaluator_settings.output / "campaign-manifest.json").read_text()
    )
    assert evaluator_manifest["status"] == "incomplete"
    assert not (evaluator_settings.output / "detection-benchmark.json").exists()
    assert evaluator_inspector.requests == []


def test_pilot_target_evaluator_contract_error_stops_before_official_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    backend = FakeBackend()

    def fail_target_evaluation(binding, project_root, transitions):
        raise ValueError("invalid evaluator contract")

    monkeypatch.setattr(campaign, "_evaluate_binding", fail_target_evaluation)
    with pytest.raises(campaign.CampaignExecutionError, match="campaign execution failed"):
        campaign.run_detection_campaign(
            settings,
            backend=backend,
            inspector=FakeInspector(),
        )

    assert backend.replays == []
    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    assert manifest["status"] == "incomplete"


def test_nonempty_output_without_exact_checkpoint_is_rejected_before_writing(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    settings.output.mkdir(parents=True)
    stale = settings.output / "stale-audit.json"
    stale.write_text("{}\n", encoding="utf-8")

    with pytest.raises(campaign.CampaignContractError, match="non-empty output"):
        campaign.run_detection_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    assert stale.read_text(encoding="utf-8") == "{}\n"
    assert not (settings.output / "checkpoint.json").exists()
    assert not (settings.output / "campaign-manifest.json").exists()


def test_failed_resume_invalidates_previously_published_scores(tmp_path: Path) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )
    stale_reports = (
        settings.output / "detection-benchmark.json",
        settings.output / "detection-benchmark.ko.md",
        settings.output / "metrics" / "official.json",
    )
    assert all(path.is_file() for path in stale_reports)
    mutated_episode = next((settings.output / "traces" / "official").rglob("episode-result.json"))
    mutated_episode.write_text('{"transitions": []}\n', encoding="utf-8")

    class FailedResumeBackend(FakeBackend):
        def run_replay(self, spec, replay, output_dir):
            raise RuntimeError("replay rerun failed")

    with pytest.raises(campaign.CampaignExecutionError, match="campaign execution failed"):
        campaign.run_detection_campaign(
            settings,
            backend=FailedResumeBackend(),
            inspector=FakeInspector(),
        )

    manifest = json.loads((settings.output / "campaign-manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert all(not path.exists() for path in stale_reports)
    archived = list((settings.output / ".superseded-results").rglob("detection-benchmark.json"))
    assert len(archived) == 1


def test_mid_publication_failure_removes_every_partial_score_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    original_write_text = Path.write_text

    def fail_after_first_score(self: Path, data: str, *args: Any, **kwargs: Any):
        if "official.ko.md" in self.name:
            raise OSError("publication failed at /private/path token=fake-secret")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_after_first_score)

    with pytest.raises(campaign.CampaignExecutionError):
        campaign.run_detection_campaign(
            settings,
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"]["status"] == "publication_cleanup_failure"
    assert not (settings.output / "detection-benchmark.json").exists()
    assert not (settings.output / "detection-benchmark.ko.md").exists()
    assert not (settings.output / "metrics").exists()
    assert "/private/path" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_archive_failure_invalidates_stale_scores_and_sanitizes_public_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )

    def fail_archive(output: Path) -> None:
        raise OSError("archive failed at /private/path token=fake-secret")

    monkeypatch.setattr(campaign, "_supersede_published_results", fail_archive)

    with pytest.raises(campaign.CampaignExecutionError):
        campaign.run_detection_campaign(
            settings,
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
    assert not (settings.output / "detection-benchmark.json").exists()
    assert not (settings.output / "detection-benchmark.ko.md").exists()
    assert not (settings.output / "metrics").exists()
    assert "/private/path" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_initialization_running_manifest_failure_invalidates_stale_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(
        settings,
        backend=FakeBackend(),
        inspector=FakeInspector(),
    )

    def fail_running_manifest(**_kwargs: Any) -> None:
        raise OSError("running manifest failed at /private/path token=fake-secret")

    monkeypatch.setattr(campaign, "_write_running_manifest", fail_running_manifest)

    with pytest.raises(OSError):
        campaign.record_detection_initialization_failure(
            settings,
            ValueError("model initialization failed"),
        )

    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"] == {
        "status": "publication_cleanup_failure",
        "error_type": "OSError",
    }
    assert not (settings.output / "detection-benchmark.json").exists()
    assert not (settings.output / "detection-benchmark.ko.md").exists()
    assert not (settings.output / "metrics").exists()
    assert "/private/path" not in manifest_text
    assert "fake-secret" not in manifest_text


def test_exact_identity_binds_schema_request_policy_scoring_and_pass_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path)
    build_hash = campaign.hash_path(settings.build)
    baseline_hash = campaign._campaign_hash(settings, build_hash)

    inspected_sources: list[str] = []
    inspection_cache_clear = getattr(
        getattr(campaign, "_inspection_contract_source_hashes", None),
        "cache_clear",
        lambda: None,
    )
    inspection_cache_clear()
    with monkeypatch.context() as source_patch:
        source_patch.setattr(
            campaign,
            "_file_hash",
            lambda path: inspected_sources.append(path.name) or "d" * 64,
        )
        campaign._inspection_request_contract_digest()
    inspection_cache_clear()
    assert set(inspected_sources) == {
        "detection_campaign.py",
        "inspector.py",
        "memory.py",
        "planners.py",
        "state_channels.py",
    }

    scoring_sources: list[str] = []
    scoring_cache_clear = getattr(
        getattr(campaign, "_scoring_contract_source_hashes", None),
        "cache_clear",
        lambda: None,
    )
    scoring_cache_clear()
    with monkeypatch.context() as source_patch:
        source_patch.setattr(
            campaign,
            "_file_hash",
            lambda path: scoring_sources.append(path.name) or "e" * 64,
        )
        campaign._scoring_contract_digest()
    scoring_cache_clear()
    assert set(scoring_sources) == {
        "detection_benchmark.py",
        "evaluation.py",
        "scenarios.py",
    }

    with monkeypatch.context() as schema_patch:
        schema_patch.setattr(
            campaign,
            "FINDINGS_SCHEMA",
            {**campaign.FINDINGS_SCHEMA, "title": "changed-inspection-schema"},
        )
        assert campaign._campaign_hash(settings, build_hash) != baseline_hash

    with monkeypatch.context() as policy_patch:
        policy_patch.setattr(campaign, "MAX_COMPLETION_REQUESTS", 3)
        assert campaign._campaign_hash(settings, build_hash) != baseline_hash

    checkpoint_path = settings.output / "identity-checkpoint.json"
    campaign.CheckpointStore(checkpoint_path, baseline_hash)
    monkeypatch.setattr(
        campaign,
        "_scoring_contract_digest",
        lambda: "changed-scoring-contract-digest",
    )
    changed_hash = campaign._campaign_hash(settings, build_hash)
    assert changed_hash != baseline_hash
    with pytest.raises(campaign.CampaignContractError, match="campaign hash mismatch"):
        campaign.CheckpointStore(checkpoint_path, changed_hash)

    pass_checkpoint = campaign.CheckpointStore(
        settings.output / "pass-checkpoint.json", "fixed-campaign"
    )
    inspector = FakeInspector()
    campaign.inspect_trace_pass(
        trace_id="opaque-contract",
        pass_index=1,
        transitions=[transition(0)],
        output_dir=settings.output / "pass-contract",
        inspector=inspector,
        checkpoint=pass_checkpoint,
        budget=campaign.InspectionCallBudget(),
        input_hash="fixed-input",
    )
    monkeypatch.setattr(
        campaign,
        "FINDINGS_SCHEMA",
        {**campaign.FINDINGS_SCHEMA, "title": "changed-pass-schema"},
    )
    with pytest.raises(campaign.CampaignContractError, match="input hash mismatch"):
        campaign.inspect_trace_pass(
            trace_id="opaque-contract",
            pass_index=1,
            transitions=[transition(0)],
            output_dir=settings.output / "pass-contract",
            inspector=inspector,
            checkpoint=pass_checkpoint,
            budget=campaign.InspectionCallBudget(),
            input_hash="fixed-input",
        )


def test_contract_source_hashes_are_frozen_for_the_loaded_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def count_hash_reads(path: Path) -> str:
        reads.append(path.name)
        return hashlib.sha256(path.name.encode("utf-8")).hexdigest()

    inspection_cache = getattr(
        campaign, "_inspection_contract_source_hashes", lambda: None
    )
    scoring_cache = getattr(campaign, "_scoring_contract_source_hashes", lambda: None)
    getattr(inspection_cache, "cache_clear", lambda: None)()
    getattr(scoring_cache, "cache_clear", lambda: None)()
    monkeypatch.setattr(campaign, "_file_hash", count_hash_reads)

    first_inspection = campaign._inspection_request_contract_digest()
    first_scoring = campaign._scoring_contract_digest()
    second_inspection = campaign._inspection_request_contract_digest()
    second_scoring = campaign._scoring_contract_digest()

    assert first_inspection == second_inspection
    assert first_scoring == second_scoring
    assert len(reads) == len(set(reads)) == 8
    getattr(inspection_cache, "cache_clear", lambda: None)()
    getattr(scoring_cache, "cache_clear", lambda: None)()


def test_resume_rejects_exact_hash_and_replay_artifact_mismatches(tmp_path: Path) -> None:
    settings = config(tmp_path)
    campaign.run_detection_campaign(settings, backend=FakeBackend(), inspector=FakeInspector())

    with pytest.raises(campaign.CampaignContractError, match="campaign hash mismatch"):
        campaign.run_detection_campaign(
            replace(settings, headless=False),
            backend=FakeBackend(),
            inspector=FakeInspector(),
        )

    settings.build.write_bytes(b"different-build")
    with pytest.raises(campaign.CampaignContractError, match="campaign hash mismatch"):
        campaign.run_detection_campaign(settings, backend=FakeBackend(), inspector=FakeInspector())


def test_cli_parses_and_dispatches_benchmark_detection_options(tmp_path: Path) -> None:
    parsed = parse_cli(
        [
            "benchmark-detection",
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
        ]
    )

    assert parsed.command == "benchmark-detection"
    assert parsed.build == tmp_path / "game"
    assert parsed.output == tmp_path / "out"
    assert parsed.api_url == "https://example.invalid/v1/chat/completions"
    assert parsed.headless is True
    assert parsed.quiet is True
    assert not hasattr(parsed, "model")


def test_benchmark_detection_cli_returns_nonzero_for_incomplete_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QA_API_KEY", "test-key")
    settings = config(tmp_path)
    parsed = parse_cli(
        [
            "benchmark-detection",
            "--build",
            str(settings.build),
            "--project-root",
            str(settings.project_root),
            "--output",
            str(settings.output),
        ]
    )

    def fail_campaign(*args, **kwargs):
        raise campaign.CampaignExecutionError("pilot execution failed")

    monkeypatch.setattr(campaign, "run_detection_campaign", fail_campaign)

    assert cli_module._benchmark_detection(parsed) == 2


def test_benchmark_detection_cli_sanitizes_inspector_initialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = config(tmp_path)
    parsed = parse_cli(
        [
            "benchmark-detection",
            "--build",
            str(settings.build),
            "--project-root",
            str(settings.project_root),
            "--output",
            str(settings.output),
        ]
    )
    secret = "credential=fake-secret at /private/provider"

    def fail_inspector(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(campaign, "LLMInspectorAdapter", fail_inspector)

    assert cli_module._benchmark_detection(parsed) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "incomplete",
        "error_type": "ValueError",
    }
    assert "Traceback" not in captured.err
    assert secret not in captured.err
    manifest_text = (settings.output / "campaign-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "incomplete"
    assert manifest["failure"] == {
        "status": "model_failure",
        "error_type": "ValueError",
    }
    assert secret not in manifest_text


def test_bridge_backend_reuses_run_session_for_pilot_and_blind_llm_autonomous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QA_INSPECTOR_MODEL", "environment-inspector-must-not-run")
    settings = config(tmp_path)
    parsed_arguments: list[list[str]] = []

    def fake_parse(arguments: list[str]):
        parsed_arguments.append(arguments)
        output = Path(arguments[arguments.index("--output") + 1])
        return SimpleNamespace(output=output)

    def fake_run(args) -> int:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "steps.jsonl").write_text(
            json.dumps(transition(0, action="wait")) + "\n", encoding="utf-8"
        )
        (args.output / "verdict.json").write_text(
            json.dumps({"execution_status": "completed"}), encoding="utf-8"
        )
        return 0

    backend = campaign.BridgeCampaignBackend(
        settings,
        parse_run_arguments=fake_parse,
        run_session_fn=fake_run,
    )
    binding = campaign.load_fault_bindings(settings.project_root)[0]
    schedule = campaign.build_track_b_schedule(campaign.load_fault_bindings(settings.project_root))
    pilot = schedule.pilots[0]
    autonomous = schedule.autonomous_pairs[0].fault
    assert isinstance(autonomous, campaign.AutonomousEpisodeSpec)

    pilot_result = backend.run_pilot(pilot, tmp_path / "pilot")
    autonomous_result = backend.run_autonomous(autonomous, tmp_path / "autonomous")

    assert pilot_result.execution_status == "completed"
    assert autonomous_result.execution_status == "completed"
    pilot_args, autonomous_args = parsed_arguments
    assert pilot_args[pilot_args.index("--policy") + 1] == "heuristic"
    assert pilot_args[pilot_args.index("--scenario") + 1] == binding.legacy_scenario_id
    assert "--fault" not in pilot_args
    assert autonomous_args[autonomous_args.index("--policy") + 1] == "llm"
    assert autonomous_args[autonomous_args.index("--model") + 1] == "gpt-4o-mini"
    assert autonomous_args[autonomous_args.index("--objective") + 1] == campaign.NEUTRAL_REACHABILITY_GOAL
    assert autonomous_args[autonomous_args.index("--max-source-steps") + 1] == "0"
    assert autonomous_args[autonomous_args.index("--fault") + 1] == autonomous.fault_id
    assert pilot_args[pilot_args.index("--inspector-model") + 1] == ""
    assert autonomous_args[autonomous_args.index("--inspector-model") + 1] == ""


def test_bridge_backend_replay_executes_the_same_commands_without_forcing_phase_sync(
    tmp_path: Path,
) -> None:
    class FakeGameAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.started: tuple[int, str, list[str]] | None = None
            self.commands: list[tuple[str, dict[str, Any]]] = []

        def start(self, *, seed: int, preset: str, faults: list[str]):
            self.started = (seed, preset, faults)
            return {"ready": True}

        def command(self, action: str, **arguments: Any):
            self.commands.append((action, arguments))
            phase = "main_menu" if len(self.commands) == 3 else "active_gameplay"
            return observation(f"replay-{len(self.commands)}", phase=phase)

        def stop(self):
            return SimpleNamespace(kind="normal", detail="")

    created: list[FakeGameAdapter] = []

    def adapter_factory(**kwargs):
        adapter = FakeGameAdapter(**kwargs)
        created.append(adapter)
        return adapter

    settings = config(tmp_path)
    backend = campaign.BridgeCampaignBackend(settings, adapter_factory=adapter_factory)
    spec = campaign.build_track_b_schedule(
        campaign.load_fault_bindings(settings.project_root)
    ).official_pairs[0].fault
    replay = campaign.build_action_replay(
        replay_id="pilot",
        scenario_id=spec.scenario_id,
        seed=spec.seed,
        build_hash=campaign.hash_path(settings.build),
        transitions=[transition(0, action="wait"), transition(1, action="wait")],
        target_command_index=0,
    )

    result = backend.run_replay(spec, replay, tmp_path / "replay")

    public_run_id = campaign.official_bridge_run_id(
        campaign.hash_path(settings.build), spec
    )
    assert created[0].kwargs["run_id"] == public_run_id
    assert created[0].started == (spec.seed, spec.preset, [spec.fault_id])
    assert [action for action, _ in created[0].commands] == ["observe", "wait", "wait"]
    assert created[0].commands[0][1]["decision_id"] == f"{public_run_id}-bootstrap"
    assert created[0].commands[1][1]["decision_id"] == f"{public_run_id}-00000000"
    assert created[0].commands[2][1]["decision_id"] == f"{public_run_id}-00000001"
    assert spec.fault_id not in json.dumps(created[0].commands)
    assert result.replay_divergence_index == 1
    assert len(result.transitions) == 2
    assert result.transitions[1]["observation"]["phase"] == "main_menu"


def test_replay_detects_changed_upgrade_menu_before_target_without_forcing_sync(
    tmp_path: Path,
) -> None:
    expected_menu = observation("pilot-menu", phase="upgrade_selection")
    expected_menu["menu"] = {
        "upgrade_open": True,
        "choices": [
            {"index": 0, "name": "Grenade"},
            {"index": 1, "name": "Armor+", "level": 1, "owned": True},
        ],
    }
    pilot_before = transition(0, action="wait", phase="upgrade_selection")
    pilot_before["observation"] = expected_menu
    pilot_selection = transition(1, action="select_upgrade", phase="active_gameplay")

    class ChangedMenuAdapter:
        def __init__(self, **kwargs) -> None:
            self.commands: list[str] = []

        def start(self, *, seed: int, preset: str, faults: list[str]):
            return {"ready": True}

        def command(self, action: str, **arguments: Any):
            self.commands.append(action)
            if len(self.commands) == 1:
                return observation("bootstrap", phase="active_gameplay")
            if len(self.commands) == 2:
                changed = observation("changed-menu", phase="upgrade_selection")
                changed["menu"] = {
                    "upgrade_open": True,
                    "choices": [
                        {"index": 0, "name": "Grenade"},
                        {"index": 1, "name": "Armor+", "level": 2, "owned": True},
                    ],
                }
                return changed
            return observation("selected", phase="active_gameplay")

        def stop(self):
            return SimpleNamespace(kind="normal", detail="")

    created: list[ChangedMenuAdapter] = []

    def adapter_factory(**kwargs):
        adapter = ChangedMenuAdapter(**kwargs)
        created.append(adapter)
        return adapter

    settings = config(tmp_path)
    backend = campaign.BridgeCampaignBackend(settings, adapter_factory=adapter_factory)
    spec = campaign.build_track_b_schedule(
        campaign.load_fault_bindings(settings.project_root)
    ).official_pairs[0].clean
    replay = campaign.build_action_replay(
        replay_id="pilot-menu",
        scenario_id=spec.scenario_id,
        seed=spec.seed,
        build_hash=campaign.hash_path(settings.build),
        transitions=[pilot_before, pilot_selection],
        target_command_index=1,
    )

    result = backend.run_replay(spec, replay, tmp_path / "changed-menu-replay")
    classified = campaign.apply_replay_divergence(result, replay)

    selection = replay["commands"][1]["semantic_selection"]
    assert selection["expected_menu_selection"] == {
        "index": 1,
        "name": "Armor+",
        "level": 1,
        "owned": True,
    }
    assert selection["expected_menu_digest"]
    assert created[0].commands == ["observe", "wait", "select_upgrade"]
    assert result.replay_divergence_index == 1
    assert result.replay_divergence_stage == "before_command"
    assert classified.coverage_override == "not_reached"
    assert classified.divergence_evidence["classification"] == "pre_target"
