from __future__ import annotations

import json
from pathlib import Path

from . import detection_campaign as detection
from . import exploration_campaign as exploration
from .detection_benchmark import (
    TraceScore,
    build_benchmark_report,
    render_benchmark_markdown,
)
from .screenshot_evidence import finalize_evidence_screenshot


def _observation(identifier: str = "obs-0000") -> dict[str, object]:
    return {
        "observation_id": identifier,
        "phase": "active_gameplay",
        "player": {"health": 10.0, "max_health": 10.0, "health_ratio": 1.0},
        "player_view": {"health": 10.0, "max_health": 10.0, "health_ratio": 1.0},
        "world": {"qa_entities": []},
        "progress": {"level_time": 1.0},
        "menu": {},
        "event_state": {},
    }


def _record(
    *,
    planner: bool = False,
    inspector: bool = False,
    oracle: bool = False,
) -> exploration.ExplorationTraceRecord:
    decision: dict[str, object] = {
        "tool": "game",
        "action": "direct_steer",
        "arguments": {"x": 1.0, "y": 0.0, "duration": 1.0},
    }
    if planner:
        decision.update(
            {
                "qa_observation": "Unexpected behavior.",
                "reflection": {
                    "status": "unexpected",
                    "summary": "Unexpected behavior.",
                    "candidate_id": "stalled-progress",
                    "evidence_refs": ["obs-0000"],
                },
            }
        )
    passes = [
        {
            "schema_version": "qa-inspection/v2",
            "findings": (
                [
                    {
                        "kind": "behavior",
                        "rule": "stalled-progress",
                        "expected_value": "progress",
                        "observed_value": "stalled",
                        "statement": "Progress stalled.",
                        "evidence_refs": ["obs-0000"],
                    }
                ]
                if inspector
                else []
            ),
        }
    ] * 3
    validations = (
        [
            {
                "category": "behavior",
                "rule": "hang",
                "field": "",
                "phase": "active_gameplay",
                "event": "",
                "statement": "The run hung.",
                "evidence_refs": ["obs-0000"],
                "emit_candidate": True,
            }
        ]
        if oracle
        else []
    )
    spec = exploration.build_track_a_schedule("poc")[0]
    return exploration.ExplorationTraceRecord(
        spec=spec,
        opaque_trace_id="8a4f12c9d3017eab",
        result=exploration.ExplorationEpisodeResult(
            transitions=[{"step": 0, "decision": decision, "observation": _observation()}],
            invariant_validations=validations,
        ),
        inspection_passes=passes,
    )


def _score(
    *,
    status: str,
    target: bool = False,
    incidental: bool = False,
) -> TraceScore:
    return TraceScore(
        trace_id="8a4f12c9d3017eab",
        fault_id="private-fault-id",
        variant="fault" if status in {"TP", "FN"} else "clean",
        status=status,  # type: ignore[arg-type]
        target_findings=[{"kind": "numeric"}] if target else [],
        incidental_candidates=[{"kind": "behavior"}] if incidental else [],
    )


def test_track_a_retention_matrix_uses_only_evidence_valid_occurrences() -> None:
    assert exploration._screenshot_retention_axes(_record(planner=True)) == ("planner",)
    assert exploration._screenshot_retention_axes(_record(inspector=True)) == ("inspector",)
    assert exploration._screenshot_retention_axes(_record(oracle=True)) == (
        "runtime_oracle",
    )
    assert exploration._screenshot_retention_axes(_record()) == ()


def test_track_b_retention_matrix_covers_tp_fn_fp_tn_and_incidental_findings() -> None:
    assert detection._screenshot_retention_axes("fail", _score(status="TP", target=True)) == (
        "oracle",
        "inspector",
    )
    assert detection._screenshot_retention_axes("fail", _score(status="FN")) == (
        "oracle",
    )
    assert detection._screenshot_retention_axes("pass", _score(status="FP", target=True)) == (
        "inspector",
    )
    assert detection._screenshot_retention_axes("pass", _score(status="TN")) == ()
    assert detection._screenshot_retention_axes(
        "pass", _score(status="TN", incidental=True)
    ) == ("inspector",)


def test_finalize_retains_with_opaque_name_or_deletes_pending(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces" / "private-fault-id" / "clean"
    trace_dir.mkdir(parents=True)
    pending = trace_dir / "final-frame.pending.png"
    pending.write_bytes(b"retained-image")

    retained = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path="final-frame.pending.png",
        screenshot_error="",
        retention_axes=("inspector",),
    )

    assert retained.path == "screenshots/8a4f12c9d3017eab.png"
    assert retained.retention_axes == ("inspector",)
    assert not pending.exists()
    assert (tmp_path / retained.path).read_bytes() == b"retained-image"
    assert "private-fault-id" not in retained.path

    resumed = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path=retained.path,
        screenshot_error="",
        retention_axes=("inspector",),
    )
    assert resumed == retained
    assert (tmp_path / retained.path).read_bytes() == b"retained-image"

    pending.write_bytes(b"discarded-image")
    stale = tmp_path / "screenshots" / "7de220db31a60fc4.png"
    stale.write_bytes(b"stale-image")
    discarded = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="7de220db31a60fc4",
        screenshot_path="final-frame.pending.png",
        screenshot_error="",
        retention_axes=(),
    )
    assert discarded.path is None
    assert not pending.exists()
    assert not stale.exists()


def test_rescoring_without_anomaly_removes_resumed_retained_screenshot(
    tmp_path: Path,
) -> None:
    retained = tmp_path / "screenshots" / "8a4f12c9d3017eab.png"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"stale-alert-frame")

    finalized = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=tmp_path / "traces" / "trace",
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path="screenshots/8a4f12c9d3017eab.png",
        screenshot_error="",
        retention_axes=(),
    )

    assert finalized.path is None
    assert finalized.retention_axes == ()
    assert not retained.exists()


def test_capture_failure_is_nonfatal_and_reports_sanitized_error(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces" / "trace"
    trace_dir.mkdir(parents=True)

    finalized = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path="final-frame.pending.png",
        screenshot_error="secret=/private/path",
        retention_axes=("oracle",),
    )

    assert finalized.path is None
    assert finalized.error == "Exception"
    assert finalized.retention_axes == ("oracle",)


def test_metadata_write_failure_removes_already_captured_pending_frame(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces" / "scored-trace"
    trace_dir.mkdir(parents=True)
    pending = trace_dir / "final-frame.pending.png"
    pending.write_bytes(b"captured-before-metadata-write-failed")

    finalized = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path=None,
        screenshot_error="OSError",
        retention_axes=("oracle",),
    )

    assert finalized.path is None
    assert finalized.error == "OSError"
    assert finalized.retention_axes == ("oracle",)
    assert not pending.exists()


def test_pending_cleanup_error_is_sanitized_and_nonfatal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trace_dir = tmp_path / "traces" / "scored-trace"
    trace_dir.mkdir(parents=True)
    pending = trace_dir / "final-frame.pending.png"
    pending.write_bytes(b"captured-before-cleanup-failed")
    original_unlink = Path.unlink

    def failing_unlink(path: Path, *args, **kwargs) -> None:
        if path == pending:
            raise OSError("secret=/private/pending-cleanup")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    finalized = finalize_evidence_screenshot(
        campaign_root=tmp_path,
        trace_output_dir=trace_dir,
        opaque_trace_id="8a4f12c9d3017eab",
        screenshot_path=None,
        screenshot_error="",
        retention_axes=("oracle",),
    )

    assert finalized.path is None
    assert finalized.error == "OSError"
    assert finalized.retention_axes == ("oracle",)


def test_scored_session_loaders_require_final_screenshot_metadata(
    tmp_path: Path,
) -> None:
    output = tmp_path / "scored-trace"
    output.mkdir()
    transition = {
        "step": 0,
        "decision": {"tool": "game", "action": "observe", "arguments": {}},
        "observation": _observation(),
    }
    (output / "steps.jsonl").write_text(json.dumps(transition) + "\n", encoding="utf-8")
    (output / "report.json").write_text(
        json.dumps({"fatal_error": "", "rule_based_anomalies": []}),
        encoding="utf-8",
    )
    (output / "verdict.json").write_text(
        json.dumps({"execution_status": "completed"}), encoding="utf-8"
    )
    (output / "run.json").write_text(
        json.dumps({"arguments": {"fault": ""}}), encoding="utf-8"
    )

    pilot_compatible = detection.BridgeCampaignBackend._load_session_result(output)
    track_a = exploration.BridgeExplorationBackend._load_result(
        exploration.build_track_a_schedule("poc")[0], output
    )
    track_b = detection.BridgeCampaignBackend._load_session_result(
        output, require_final_screenshot=True
    )

    assert track_a.execution_status == "completed"
    assert track_b.execution_status == "completed"
    assert pilot_compatible.screenshot_error == ""
    assert track_a.screenshot_path is None
    assert track_b.screenshot_path is None
    assert track_a.screenshot_error == "FileNotFoundError"
    assert track_b.screenshot_error == "FileNotFoundError"

    record = exploration.ExplorationTraceRecord(
        spec=exploration.build_track_a_schedule("poc")[0],
        opaque_trace_id="8a4f12c9d3017eab",
        result=track_a,
        inspection_passes=(
            {"schema_version": "qa-inspection/v2", "findings": []},
        )
        * 3,
    )
    assert exploration.build_exploration_report([record], metadata={})["summary"][
        "capture_error_count"
    ] == 1

    pair = detection.PairScore(
        pair_id="private-pair",
        fault_id="private-fault-id",
        status="VALID",
        clean=_score(status="TN"),
        fault=_score(status="FN"),
    )
    pair.clean.screenshot_error = track_b.screenshot_error
    assert build_benchmark_report([pair], metadata={})["screenshot_summary"][
        "capture_error_count"
    ] == 1


def test_inspector_requests_never_include_final_screenshot_material(
    tmp_path: Path,
) -> None:
    observation = _observation()
    observation.update(
        {
            "screenshot_path": "final-frame.pending.png",
            "image_data": "base64-secret-image-data",
            "image": {"bytes": "raw-secret-image-bytes"},
        }
    )
    transitions = [
        {
            "step": 0,
            "decision": {
                "tool": "game",
                "action": "observe",
                "arguments": {},
                "screenshot_path": "final-frame.pending.png",
            },
            "observation": observation,
            "screenshot_path": "final-frame.pending.png",
        }
    ]

    class Inspector:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def inspect(
            self, system_prompt: str, payload: dict[str, object]
        ) -> detection.InspectionResponse:
            self.requests.append(json.loads(json.dumps(payload)))
            return detection.InspectionResponse(
                raw_response={"schema_version": "qa-inspection/v2", "findings": []},
                usage={"llm_http_attempts": 1, "llm_completion_requests": 1},
            )

    inspector = Inspector()
    checkpoint = detection.CheckpointStore(tmp_path / "checkpoint.json", "campaign")
    budget = detection.InspectionCallBudget(cap=3)
    for pass_index in (1, 2, 3):
        detection.inspect_trace_pass(
            trace_id="8a4f12c9d3017eab",
            pass_index=pass_index,
            transitions=transitions,
            output_dir=tmp_path / "inspections",
            inspector=inspector,
            checkpoint=checkpoint,
            budget=budget,
            input_hash="input",
        )

    assert len(inspector.requests) == 3
    serialized = json.dumps(inspector.requests, ensure_ascii=False).lower()
    for marker in (
        "screenshot_path",
        "final-frame.pending.png",
        "image_data",
        '"image"',
        '"bytes"',
        "base64-secret-image-data",
        "raw-secret-image-bytes",
    ):
        assert marker not in serialized


def test_reports_publish_relative_refs_axes_and_capture_error_counts() -> None:
    record = _record(planner=True)
    record = exploration.ExplorationTraceRecord(
        spec=record.spec,
        opaque_trace_id=record.opaque_trace_id,
        result=exploration.ExplorationEpisodeResult(
            transitions=record.result.transitions,
            screenshot_path="screenshots/8a4f12c9d3017eab.png",
            screenshot_error="",
            screenshot_retention_axes=("planner",),
        ),
        inspection_passes=record.inspection_passes,
    )
    exploration_report = exploration.build_exploration_report([record], metadata={})
    exploration_markdown = exploration.render_exploration_markdown(exploration_report)
    assert exploration_report["summary"]["capture_error_count"] == 0
    assert exploration_report["screenshots"] == [
        {
            "trace_id": "8a4f12c9d3017eab",
            "path": "screenshots/8a4f12c9d3017eab.png",
            "retention_axes": ["planner"],
            "capture_error": "",
        }
    ]
    assert "screenshots/8a4f12c9d3017eab.png" in exploration_markdown

    pair = detection.PairScore(
        pair_id="private-pair",
        fault_id="private-fault-id",
        status="VALID",
        clean=_score(status="FP", target=True),
        fault=_score(status="FN"),
    )
    pair.clean.screenshot_path = "screenshots/8a4f12c9d3017eab.png"
    pair.clean.screenshot_retention_axes = ["inspector"]
    pair.fault.screenshot_error = "OSError"
    pair.fault.screenshot_retention_axes = ["oracle"]
    benchmark_report = build_benchmark_report([pair], metadata={})
    benchmark_markdown = render_benchmark_markdown(benchmark_report)
    assert benchmark_report["screenshot_summary"] == {
        "retained_count": 1,
        "capture_error_count": 1,
    }
    assert "screenshots/8a4f12c9d3017eab.png" in benchmark_markdown
    assert "캡처 오류: 1" in benchmark_markdown
    assert "private-fault-id" not in json.dumps(
        benchmark_report["screenshots"], ensure_ascii=False
    )


def test_checkpoint_binds_retained_png_and_rejects_tampering(tmp_path: Path) -> None:
    checkpoint = detection.CheckpointStore(tmp_path / "checkpoint.json", "campaign")
    episode_path = tmp_path / "traces" / "unit" / "episode-result.json"
    screenshot = tmp_path / "screenshots" / "8a4f12c9d3017eab.png"
    episode_path.parent.mkdir(parents=True)
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"original")
    episode = detection.EpisodeResult(
        transitions=[{"observation": _observation()}],
        screenshot_path="screenshots/8a4f12c9d3017eab.png",
        screenshot_retention_axes=("oracle",),
    )
    episode_path.write_text(json.dumps(episode.to_dict()), encoding="utf-8")
    checkpoint.mark_complete("episode/unit", "input", [episode_path, screenshot])

    called = 0

    def execute() -> detection.EpisodeResult:
        nonlocal called
        called += 1
        return detection.EpisodeResult(transitions=[{"observation": _observation()}])

    restored, resumed = detection._run_episode_unit(
        checkpoint=checkpoint,
        unit_id="episode/unit",
        input_hash="input",
        path=episode_path,
        execute=execute,
        artifact_root=tmp_path,
    )
    assert resumed is True
    assert restored.screenshot_path == "screenshots/8a4f12c9d3017eab.png"
    assert called == 0

    screenshot.write_bytes(b"tampered")
    restored, resumed = detection._run_episode_unit(
        checkpoint=checkpoint,
        unit_id="episode/unit",
        input_hash="input",
        path=episode_path,
        execute=execute,
        artifact_root=tmp_path,
    )
    assert resumed is False
    assert restored.screenshot_path is None
    assert called == 1


def test_track_a_checkpoint_resumes_retained_png_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    spec = exploration.build_track_a_schedule("poc")[0]
    campaign_hash = "campaign"
    checkpoint = detection.CheckpointStore(tmp_path / "checkpoint.json", campaign_hash)
    directory = tmp_path / "traces" / spec.mission_id / str(spec.seed)
    episode_path = directory / "episode-result.json"
    launch_path = directory / "launch-manifest.json"
    screenshot = tmp_path / "screenshots" / "8a4f12c9d3017eab.png"
    directory.mkdir(parents=True)
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"original")
    episode = exploration.ExplorationEpisodeResult(
        transitions=[{"observation": _observation()}],
        screenshot_path="screenshots/8a4f12c9d3017eab.png",
        screenshot_retention_axes=("planner",),
    )
    episode_path.write_text(json.dumps(episode.to_dict()), encoding="utf-8")
    launch_path.write_text("{}", encoding="utf-8")
    input_hash = exploration._episode_hash(campaign_hash, spec)
    checkpoint.mark_complete(
        spec.unit_id,
        input_hash,
        [episode_path, launch_path, screenshot],
    )

    class Backend:
        calls = 0

        def run_autonomous(
            self,
            _spec: exploration.ExplorationEpisodeSpec,
            _output_dir: Path,
        ) -> exploration.ExplorationEpisodeResult:
            self.calls += 1
            return exploration.ExplorationEpisodeResult(
                transitions=[{"observation": _observation()}]
            )

    backend = Backend()
    restored, resumed = exploration._run_episode(
        spec=spec,
        output=tmp_path,
        backend=backend,
        checkpoint=checkpoint,
        campaign_hash=campaign_hash,
    )
    assert resumed is True
    assert restored.screenshot_path == "screenshots/8a4f12c9d3017eab.png"
    assert backend.calls == 0

    screenshot.write_bytes(b"tampered")
    restored, resumed = exploration._run_episode(
        spec=spec,
        output=tmp_path,
        backend=backend,
        checkpoint=checkpoint,
        campaign_hash=campaign_hash,
    )
    assert resumed is False
    assert restored.screenshot_path is None
    assert backend.calls == 1
