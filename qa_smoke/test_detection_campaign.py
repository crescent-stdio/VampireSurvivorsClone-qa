from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from . import detection_campaign as campaign
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
        return campaign.EpisodeResult(
            transitions=[transition(0), transition(1)],
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
    def __init__(self, *, http_attempts: int = 1) -> None:
        self.requests: list[dict[str, Any]] = []
        self.http_attempts = http_attempts

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
                "llm_retries": max(0, self.http_attempts - 1),
                "llm_retry_wait_ms": 0,
            },
            elapsed_seconds=0.01,
        )


def config(tmp_path: Path) -> campaign.BenchmarkCampaignConfig:
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
    assert len(result.pairs) == 44
    assert all("pilot" not in pair.pair_id for pair in result.pairs)
    assert (config(tmp_path).output / "detection-benchmark.json").exists()
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
        campaign.EpisodeResult(transitions=[transition(0)], replay_divergence_index=0), replay
    )
    after = campaign.apply_replay_divergence(
        campaign.EpisodeResult(transitions=[transition(0), transition(1)], replay_divergence_index=1),
        replay,
    )

    assert before.coverage_override == "not_reached"
    assert before.oracle_override == "not_evaluated"
    assert before.divergence_evidence == {
        "classification": "pre_target",
        "command_index": 0,
        "target_command_index": 1,
    }
    assert after.coverage_override is None
    assert after.oracle_override is None
    assert after.divergence_evidence == {
        "classification": "post_target",
        "command_index": 1,
        "target_command_index": 1,
    }


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
    assert budget.logical_calls == 2


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


def test_failed_or_incomplete_checkpoint_units_are_rerun(tmp_path: Path) -> None:
    store = campaign.CheckpointStore(tmp_path / "checkpoint.json", "campaign-hash")
    store.mark_failed("pilot/example", "unit-hash", "boom")
    assert store.reusable("pilot/example", "unit-hash", [tmp_path / "pilot.json"]) is False
    store.mark_started("pilot/example", "unit-hash")
    assert store.reusable("pilot/example", "unit-hash", [tmp_path / "pilot.json"]) is False

    result_path = tmp_path / "failed-episode.json"
    result, resumed = campaign._run_episode_unit(
        checkpoint=store,
        unit_id="replay/failed",
        input_hash="replay-hash",
        path=result_path,
        execute=lambda: campaign.EpisodeResult(
            transitions=[], execution_status="infrastructure_error", error="bridge stopped"
        ),
    )
    assert result.execution_status == "infrastructure_error"
    assert resumed is False
    assert store.reusable("replay/failed", "replay-hash", [result_path]) is False


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


def test_bridge_backend_reuses_run_session_for_pilot_and_blind_llm_autonomous(
    tmp_path: Path,
) -> None:
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
    assert "--inspector-model" not in autonomous_args


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

    assert created[0].started == (spec.seed, spec.preset, [spec.fault_id])
    assert [action for action, _ in created[0].commands] == ["observe", "wait", "wait"]
    assert result.replay_divergence_index == 1
    assert len(result.transitions) == 2
    assert result.transitions[1]["observation"]["phase"] == "main_menu"
