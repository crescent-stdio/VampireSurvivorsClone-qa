from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import detection_campaign, exploration_campaign
from . import run as run_module
from .adapters import EpisodeExit, VampireSurvivorsAdapter


def _observation(identifier: str) -> dict[str, Any]:
    return {
        "ok": True,
        "observation_id": identifier,
        "phase": "active_gameplay",
        "scene": "Level 1",
        "frame": 1,
        "available_actions": ["wait"],
        "player": {"present": True, "health": 10.0, "max_health": 10.0},
        "player_view": {"health": 10.0, "max_health": 10.0},
        "progress": {"level_time": 1.0},
        "world": {},
        "menu": {},
        "inventory": {},
        "event_state": {},
    }


def _transition(index: int) -> dict[str, Any]:
    return {
        "step": index,
        "decision": {
            "tool": "game",
            "action": "wait",
            "arguments": {"duration": 1.0},
        },
        "observation": _observation(f"obs-{index:04d}"),
    }


def test_adapter_capture_returns_relative_pending_path_and_sanitizes_failure(
    tmp_path: Path,
) -> None:
    class CapturingBridge:
        def __init__(self, **kwargs: Any) -> None:
            self.session_dir = Path(kwargs["session_dir"])
            self.process = SimpleNamespace(returncode=None)

        def launch(self) -> dict[str, Any]:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            return {"ready": True}

        def command(self, action: str, **_parameters: Any) -> dict[str, Any]:
            assert action == "capture_screenshot"
            (self.session_dir / "final-frame.pending.png").write_bytes(b"png")
            return {"ok": True}

        def close(self) -> None:
            return None

    adapter = VampireSurvivorsAdapter(
        game_exe=tmp_path / "game",
        session_dir=tmp_path / "success",
        bridge_factory=CapturingBridge,
    )
    adapter.start(seed=1, preset="", faults=[])

    capture = adapter.capture_final_screenshot()

    assert capture.path == "final-frame.pending.png"
    assert capture.error == ""

    class FailingBridge(CapturingBridge):
        def command(self, action: str, **_parameters: Any) -> dict[str, Any]:
            raise RuntimeError("secret=/private/capture/token")

    failed = VampireSurvivorsAdapter(
        game_exe=tmp_path / "game",
        session_dir=tmp_path / "failure",
        bridge_factory=FailingBridge,
    )
    failed.start(seed=1, preset="", faults=[])

    capture = failed.capture_final_screenshot()

    assert capture.path is None
    assert capture.error == "RuntimeError"


def test_scored_runner_captures_before_stop_and_records_nonfatal_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ready": True}

        def command(self, action: str, **_parameters: Any) -> dict[str, Any]:
            assert action == "observe"
            return _observation("bootstrap")

        def capture_final_screenshot(self):
            events.append("capture")
            raise RuntimeError("secret=/private/capture/token")

        def stop(self) -> EpisodeExit:
            events.append("stop")
            return EpisodeExit(kind="normal")

    monkeypatch.setattr(run_module, "VampireSurvivorsAdapter", FakeAdapter)
    output = tmp_path / "run"
    args = run_module.parse_args(
        [
            "--game-exe",
            str(tmp_path / "game"),
            "--output",
            str(output),
            "--policy",
            "heuristic",
            "--max-steps",
            "0",
            "--capture-final-screenshot",
            "--quiet",
        ]
    )

    run_module.run_session(args)

    assert events == ["capture", "stop"]
    assert json.loads((output / "final-frame.json").read_text(encoding="utf-8")) == {
        "path": None,
        "screenshot_error": "RuntimeError",
    }
    verdict = json.loads((output / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["execution_status"] == "completed"


def test_track_a_and_track_b_only_request_capture_for_scored_traces(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    build = tmp_path / "game-build"
    build.write_bytes(b"build")
    parsed_arguments: list[list[str]] = []

    def fake_parse(arguments: list[str]):
        parsed_arguments.append(arguments)
        output = Path(arguments[arguments.index("--output") + 1])
        return SimpleNamespace(output=output, preset="", fault="")

    def fake_run(args) -> int:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "steps.jsonl").write_text(
            json.dumps(_transition(0)) + "\n", encoding="utf-8"
        )
        (args.output / "verdict.json").write_text(
            json.dumps({"execution_status": "completed"}), encoding="utf-8"
        )
        (args.output / "report.json").write_text(
            json.dumps({"fatal_error": None, "rule_based_anomalies": []}),
            encoding="utf-8",
        )
        (args.output / "run.json").write_text(
            json.dumps({"arguments": {"fault": ""}}), encoding="utf-8"
        )
        arguments = parsed_arguments[-1]
        if "--capture-final-screenshot" in arguments:
            (args.output / "final-frame.json").write_text(
                json.dumps({"path": "final-frame.pending.png", "screenshot_error": ""}),
                encoding="utf-8",
            )
        return 0

    track_a_backend = exploration_campaign.BridgeExplorationBackend(
        exploration_campaign.ExplorationCampaignConfig(
            build=build,
            project_root=project_root,
            output=tmp_path / "track-a",
        ),
        parse_run_arguments=fake_parse,
        run_session_fn=fake_run,
    )
    track_a_result = track_a_backend.run_autonomous(
        exploration_campaign.build_track_a_schedule("poc")[0], tmp_path / "a-trace"
    )

    settings = detection_campaign.BenchmarkCampaignConfig(
        build=build,
        project_root=project_root,
        output=tmp_path / "track-b",
    )
    track_b_backend = detection_campaign.BridgeCampaignBackend(
        settings,
        parse_run_arguments=fake_parse,
        run_session_fn=fake_run,
    )
    schedule = detection_campaign.build_track_b_schedule(
        detection_campaign.load_fault_bindings(project_root), "poc"
    )
    pilot_result = track_b_backend.run_pilot(schedule.pilots[0], tmp_path / "pilot")
    autonomous_result = track_b_backend.run_autonomous(
        schedule.autonomous_pairs[0].fault, tmp_path / "b-autonomous"
    )

    track_a_args, pilot_args, autonomous_args = parsed_arguments
    assert "--capture-final-screenshot" in track_a_args
    assert "--capture-final-screenshot" not in pilot_args
    assert "--capture-final-screenshot" in autonomous_args
    assert track_a_result.screenshot_path == "final-frame.pending.png"
    assert pilot_result.screenshot_path is None
    assert autonomous_result.screenshot_path == "final-frame.pending.png"


def test_official_replay_captures_before_stop_and_capture_failure_is_nonfatal(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ready": True}

        def command(self, action: str, **_parameters: Any) -> dict[str, Any]:
            return _observation(action)

        def capture_final_screenshot(self):
            events.append("capture")
            return SimpleNamespace(path=None, error="RuntimeError")

        def stop(self) -> EpisodeExit:
            events.append("stop")
            return EpisodeExit(kind="normal")

    project_root = Path(__file__).resolve().parents[1]
    build = tmp_path / "build"
    build.write_bytes(b"build")
    settings = detection_campaign.BenchmarkCampaignConfig(
        build=build,
        project_root=project_root,
        output=tmp_path / "campaign",
    )
    backend = detection_campaign.BridgeCampaignBackend(
        settings, adapter_factory=FakeAdapter
    )
    spec = detection_campaign.build_track_b_schedule(
        detection_campaign.load_fault_bindings(project_root), "poc"
    ).official_pairs[0].clean
    replay = detection_campaign.build_action_replay(
        replay_id="opaque-pilot",
        scenario_id=spec.scenario_id,
        seed=spec.seed,
        build_hash=detection_campaign.hash_path(build),
        transitions=[_transition(0)],
        target_command_index=0,
    )

    result = backend.run_replay(spec, replay, tmp_path / "official")

    assert events == ["capture", "stop"]
    assert result.execution_status == "completed"
    assert result.screenshot_path is None
    assert result.screenshot_error == "RuntimeError"


def test_official_replay_stops_when_screenshot_metadata_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ready": True}

        def command(self, action: str, **_parameters: Any) -> dict[str, Any]:
            return _observation(action)

        def capture_final_screenshot(self):
            events.append("capture")
            return SimpleNamespace(path="final-frame.pending.png", error="")

        def stop(self) -> EpisodeExit:
            events.append("stop")
            return EpisodeExit(kind="normal")

    def fail_metadata_write(*_args: Any, **_kwargs: Any) -> None:
        events.append("metadata")
        raise OSError("secret=/private/screenshot-metadata")

    monkeypatch.setattr(
        detection_campaign,
        "write_final_screenshot_metadata",
        fail_metadata_write,
    )
    project_root = Path(__file__).resolve().parents[1]
    build = tmp_path / "build"
    build.write_bytes(b"build")
    backend = detection_campaign.BridgeCampaignBackend(
        detection_campaign.BenchmarkCampaignConfig(
            build=build,
            project_root=project_root,
            output=tmp_path / "campaign",
        ),
        adapter_factory=FakeAdapter,
    )
    spec = detection_campaign.build_track_b_schedule(
        detection_campaign.load_fault_bindings(project_root), "poc"
    ).official_pairs[0].clean
    replay = detection_campaign.build_action_replay(
        replay_id="opaque-pilot",
        scenario_id=spec.scenario_id,
        seed=spec.seed,
        build_hash=detection_campaign.hash_path(build),
        transitions=[_transition(0)],
        target_command_index=0,
    )

    result = backend.run_replay(spec, replay, tmp_path / "official")

    assert events == ["capture", "metadata", "stop"]
    assert result.execution_status == "completed"
    assert result.screenshot_path is None
    assert result.screenshot_error == "OSError"


def test_campaign_loaders_preserve_capture_metadata_on_trace_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "failed-trace"
    output.mkdir()
    (output / "report.json").write_text(
        json.dumps(
            {
                "fatal_error": "RuntimeError: details redacted",
                "rule_based_anomalies": [],
            }
        ),
        encoding="utf-8",
    )
    (output / "final-frame.json").write_text(
        json.dumps({"path": "final-frame.pending.png", "screenshot_error": ""}),
        encoding="utf-8",
    )

    track_a = exploration_campaign.BridgeExplorationBackend._load_result(
        exploration_campaign.build_track_a_schedule("poc")[0], output
    )
    track_b = detection_campaign.BridgeCampaignBackend._load_session_result(output)

    assert track_a.screenshot_path == "final-frame.pending.png"
    assert track_b.screenshot_path == "final-frame.pending.png"
