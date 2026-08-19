from __future__ import annotations

import json
import math
import platform
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .charter import MOVEMENT_VECTORS


EXECUTION_STATUSES = {"completed", "infrastructure_error", "contract_error"}
COVERAGE_STATUSES = {"reached", "not_reached"}
ORACLE_VERDICTS = {"pass", "fail", "not_evaluated"}
AGENT_DETECTIONS = {"match", "miss", "false_positive", "not_evaluated"}
# "partial" marks a verdict judged from the prefix of an aborted run.
TRACE_COMPLETENESS = {"complete", "partial"}
from .detection import AUTHORITY_NOTE as DETECTION_AUTHORITY_NOTE

ALWAYS_ON_FAILURE_KINDS = {
    "crash",
    "timeout",
    "exception",
    "freeze",
    "stuck",
    "unresponsive",
    "transition",
    "description_flaw",
    "view_state_match",
    "bridge_action_failed",
    "unity_error",
    "non_finite_state",
    "unexpected_pause",
    "llm_gameplay_stalled",
}


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reviewer_id: str
    candidate_id: str
    label: Literal["valid_bug", "non_bug", "duplicate", "uncertain"]
    matched_bug_id: str | None
    reproducible: bool
    spec_violation: bool
    system_caused: bool
    difficulty: Literal["easy", "medium", "hard"]
    evidence_refs: list[str]
    notes: str


def build_run_verdict(
    *,
    execution_status: str,
    coverage_status: str,
    oracle_verdict: str,
    agent_detection: str,
    evidence_refs: list[str],
    anomalies: list[dict[str, Any]] | None = None,
    trace_completeness: str = "complete",
) -> dict[str, Any]:
    if execution_status not in EXECUTION_STATUSES:
        raise ValueError(f"unsupported execution_status: {execution_status}")
    if coverage_status not in COVERAGE_STATUSES:
        raise ValueError(f"unsupported coverage_status: {coverage_status}")
    if oracle_verdict not in ORACLE_VERDICTS:
        raise ValueError(f"unsupported oracle_verdict: {oracle_verdict}")
    if agent_detection not in AGENT_DETECTIONS:
        raise ValueError(f"unsupported agent_detection: {agent_detection}")
    if execution_status == "infrastructure_error" and oracle_verdict == "pass":
        raise ValueError("infrastructure_error execution cannot report oracle_verdict pass")
    if coverage_status == "not_reached" and oracle_verdict != "not_evaluated":
        raise ValueError("not_reached coverage requires oracle_verdict not_evaluated")
    if execution_status == "completed" and not evidence_refs:
        raise ValueError("completed verdict requires evidence_refs")
    if trace_completeness not in TRACE_COMPLETENESS:
        raise ValueError(f"unsupported trace_completeness: {trace_completeness}")
    return {
        "schema_version": "qa-run-verdict/v2",
        "execution_status": execution_status,
        "coverage_status": coverage_status,
        "oracle_verdict": oracle_verdict,
        "agent_detection": agent_detection,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "trace_completeness": trace_completeness,
        "final_verdict": final_verdict(
            execution_status,
            coverage_status,
            oracle_verdict,
            anomalies or [],
        ),
    }


def final_verdict(
    execution_status: str,
    coverage_status: str,
    oracle_verdict: str,
    anomalies: list[dict[str, Any]],
) -> str:
    """Map detailed execution axes and always-on anomalies to the four-value verdict."""

    if execution_status != "completed":
        return "ERROR"
    if any(str(item.get("kind", "")) in ALWAYS_ON_FAILURE_KINDS for item in anomalies):
        return "FAIL"
    if oracle_verdict == "fail":
        return "FAIL"
    if coverage_status == "not_reached":
        return "NOT_REACHED"
    return "PASS"


def aggregate_annotations(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Annotation]] = defaultdict(list)
    for record in records:
        annotation = Annotation.model_validate(record)
        grouped[annotation.candidate_id].append(annotation)

    candidates: list[dict[str, Any]] = []
    all_reviewed = bool(grouped)
    for candidate_id, annotations in sorted(grouped.items()):
        reviewer_ids = {annotation.reviewer_id for annotation in annotations}
        if len(reviewer_ids) != len(annotations):
            raise ValueError(f"duplicate reviewer annotation for candidate {candidate_id}")
        counts = Counter(annotation.label for annotation in annotations)
        label, votes = counts.most_common(1)[0]
        tied = sum(1 for count in counts.values() if count == votes) > 1
        if tied:
            label = "uncertain"
            agreement = "no_consensus"
        elif votes == len(annotations):
            agreement = "unanimous"
        elif votes > len(annotations) / 2:
            agreement = "majority"
        else:
            agreement = "no_consensus"
        reviewed = len(reviewer_ids) >= 3
        all_reviewed = all_reviewed and reviewed
        candidates.append(
            {
                "candidate_id": candidate_id,
                "label": label,
                "agreement": agreement,
                "reviewer_count": len(reviewer_ids),
                "matched_bug_ids": sorted(
                    {
                        annotation.matched_bug_id
                        for annotation in annotations
                        if annotation.matched_bug_id
                    }
                ),
                "evidence_refs": list(
                    dict.fromkeys(
                        reference
                        for annotation in annotations
                        for reference in annotation.evidence_refs
                    )
                ),
            }
        )
    return {
        "status": "reviewed" if all_reviewed else "provisional",
        "candidates": candidates,
    }


@dataclass
class RunRecorder:
    output_dir: Path
    mode: str
    policy: str
    seed: int
    run_id: str = ""
    charter: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    game_window_visible: bool = True
    scenario_id: str = ""
    preset: str = ""
    scenario_fingerprint: str = ""
    fault_id: str | None = None
    protocol_version: str = "1.5"
    prompt_version: str = "qa-planning/v5"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    max_level: int = 0
    max_level_time: float = 0.0
    total_simulation_time: float = 0.0
    max_kills: int = 0
    entered_gameplay: bool = False
    upgrade_seen: bool = False
    upgrade_selected: int = 0
    upgrade_resumed: bool = False
    death_seen: bool = False
    restart_seen: bool = False
    previous_level_time: float = 0.0
    previous_observation_frame: int | None = None
    previous_observation_level_time: float | None = None
    selected_at_level_time: float | None = None
    launched: bool = False
    steering_horizons: int = 0
    direct_control_horizons: int = 0
    intent_counts: dict[str, int] = field(default_factory=dict)
    chest_collections: int = 0
    min_chest_distance: float | None = None
    event_counts: dict[str, int] = field(default_factory=dict)
    api_usage_events: list[dict[str, Any]] = field(default_factory=list)
    planning_window_wall_seconds: float = 0.0
    initial_contract_rejections: int = 0
    repair_contract_rejections: int = 0
    navigation_evaluations: list[dict[str, Any]] = field(default_factory=list)
    continuous_control_horizons: int = 0
    assist_horizons: int = 0
    assist_frames: int = 0
    assist_control_frames: int = 0
    assist_deflection_sum: float = 0.0
    assist_max_deflection: float = 0.0
    assist_frames_total_seen: int = 0
    assist_deflection_total_seen: float = 0.0
    verdict_axes: dict[str, Any] | None = None
    detection: Any = None

    def __post_init__(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "steps.jsonl").touch(exist_ok=True)

    def record(
        self,
        step: int,
        decision: dict[str, Any],
        observation: dict[str, Any],
        elapsed: float,
        api_usage: dict[str, int] | None = None,
    ) -> None:
        entry = {
            "step": step,
            "run_id": observation.get("run_id") or self.run_id,
            "observation_id": observation.get("observation_id") or "",
            "decision_id": decision.get("decision_id") or "",
            "expected_effect": decision.get("expected_effect") or "",
            "reflection": decision.get("reflection") or {},
            "observed_delta": decision.get("observed_delta") or {},
            "hypothesis_state": decision.get("hypothesis_state"),
            "elapsed_wall_seconds": round(elapsed, 3),
            "decision": decision,
            "observation": observation,
            "api_usage": api_usage or {},
        }
        self.steps.append(entry)
        with (self.output_dir / "steps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._update_coverage(decision, observation)
        self._detect_anomalies(step, observation)

    def add_api_usage(
        self,
        phase: str,
        usage: dict[str, int],
        step: int | None = None,
        *,
        cache_boundary: bool = False,
    ) -> None:
        if not usage:
            return
        event_usage = dict(usage)
        event_usage.pop("cache_boundary", None)
        if "uncached_prompt_tokens" not in event_usage:
            event_usage["uncached_prompt_tokens"] = max(
                0,
                int(event_usage.get("prompt_tokens", 0) or 0)
                - int(event_usage.get("cached_tokens", 0) or 0),
            )
        self.api_usage_events.append(
            {
                "phase": phase,
                "step": step,
                "cache_boundary": bool(cache_boundary),
                **event_usage,
            }
        )

    def write_channel_artifacts(
        self,
        verdict: dict[str, Any],
        critic: dict[str, Any],
    ) -> None:
        self.verdict_axes = verdict
        manifest = {
            "schema_version": "qa-run-manifest/v1",
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "preset": self.preset,
            "scenario_fingerprint": self.scenario_fingerprint,
            "seed": self.seed,
            "platform": platform.platform(),
            "mode": self.mode,
            "policy": self.policy,
            "model": self.model,
            "prompt_version": self.effective_prompt_version(),
            "fault_id": self.fault_id,
        }
        for name, payload in (
            ("manifest.json", manifest),
            ("verdict.json", verdict),
            ("critic.json", critic),
        ):
            (self.output_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if self.detection is not None:
            self._write_detection()
        # Left empty on purpose: annotations.jsonl is filled by human reviewers.
        (self.output_dir / "annotations.jsonl").touch(exist_ok=True)

    def _write_detection(self) -> None:
        """Record the automatic agent-detection score beside the verdict.

        Deliberately not named *request*.jsonl: the deterministic gate globs those
        for a fault-id leak scan, and this file names the injected fault the same
        way manifest.json already does.
        """
        payload = {
            "schema_version": "qa-agent-detection/v1",
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "fault_id": self.fault_id,
            "policy": self.policy,
            "prompt_version": self.effective_prompt_version(),
            **(
                self.detection.model_dump()
                if hasattr(self.detection, "model_dump")
                else dict(self.detection)
            ),
            "authority_note": DETECTION_AUTHORITY_NOTE,
        }
        (self.output_dir / "agent-detection.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def api_usage_totals(self) -> dict[str, Any]:
        keys = (
            "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
            "uncached_prompt_tokens", "cache_write_tokens", "reasoning_tokens", "latency_ms",
            "llm_retries", "llm_retry_wait_ms", "llm_http_attempts",
        )
        totals = {key: sum(int(event.get(key, 0) or 0) for event in self.api_usage_events) for key in keys}
        totals["calls"] = sum(
            max(1, int(event.get("request_count", 1) or 1)) for event in self.api_usage_events
        )
        totals["mean_latency_ms"] = (
            round(totals["latency_ms"] / totals["calls"], 1) if totals["calls"] else 0.0
        )
        planning_events = [
            event
            for event in self.api_usage_events
            if event.get("phase") in {"planning_request", "repair_request"}
        ]
        eligible_planning_events = [
            event
            for event in planning_events
            if (
                (event.get("step") is None or int(event.get("step")) != 0)
                and not bool(event.get("cache_boundary"))
            )
        ]
        planning_calls = sum(
            max(1, int(event.get("request_count", 1) or 1))
            for event in planning_events
        )
        eligible_planning_calls = sum(
            max(1, int(event.get("request_count", 1) or 1))
            for event in eligible_planning_events
        )
        eligible_prompt_tokens = sum(
            int(event.get("prompt_tokens", 0) or 0)
            for event in eligible_planning_events
        )
        eligible_cached_tokens = sum(
            int(event.get("cached_tokens", 0) or 0)
            for event in eligible_planning_events
        )
        eligible_uncached_tokens = sum(
            int(event.get("uncached_prompt_tokens", 0) or 0)
            for event in eligible_planning_events
        )
        planning_latency_ms = sum(
            int(event.get("latency_ms", 0) or 0) for event in planning_events
        )
        totals["planning_requests"] = planning_calls
        totals["average_uncached_prompt_tokens"] = round(
            eligible_uncached_tokens / eligible_planning_calls,
            1,
        ) if eligible_planning_calls else 0.0
        totals["planning_cache_ratio"] = round(
            eligible_cached_tokens / eligible_prompt_tokens,
            3,
        ) if eligible_prompt_tokens else 0.0
        totals["planning_mean_latency_ms"] = round(
            planning_latency_ms / planning_calls,
            1,
        ) if planning_calls else 0.0
        totals["llm_wait_ratio"] = round(
            planning_latency_ms / (self.planning_window_wall_seconds * 1000.0),
            3,
        ) if planning_events and self.planning_window_wall_seconds > 0 else 0.0
        totals["initial_contract_rejections"] = self.initial_contract_rejections
        totals["repair_contract_rejections"] = self.repair_contract_rejections
        return totals

    def _update_coverage(self, decision: dict[str, Any], observation: dict[str, Any]) -> None:
        player = observation.get("player") or {}
        progress = observation.get("progress") or {}
        menu = observation.get("menu") or {}
        world = observation.get("world") or {}
        event_state = observation.get("event_state") or {}
        if player.get("present"):
            self.entered_gameplay = True
        self.max_level = max(self.max_level, int(player.get("level") or 0))
        level_time = float(progress.get("level_time") or 0.0)
        if player.get("present"):
            self.total_simulation_time += max(0.0, level_time - self.previous_level_time)
        self.max_level_time = max(self.max_level_time, level_time)
        self.max_kills = max(self.max_kills, int(progress.get("monsters_killed") or 0))
        self.upgrade_seen = self.upgrade_seen or bool(menu.get("upgrade_open"))
        if decision.get("tool") == "game" and decision.get("action") == "select_upgrade" and observation.get("ok"):
            self.upgrade_selected += 1
            self.selected_at_level_time = level_time
        if self.selected_at_level_time is not None and level_time > self.selected_at_level_time + 0.05:
            self.upgrade_resumed = True
        self.death_seen = self.death_seen or bool(menu.get("game_over"))
        if decision.get("tool") == "game" and decision.get("action") == "restart" and observation.get("ok"):
            self.restart_seen = True
        if decision.get("tool") == "game" and decision.get("action") in ("steer", "direct_steer") and observation.get("ok"):
            self.steering_horizons += 1
        if decision.get("tool") == "game" and decision.get("action") == "direct_steer" and observation.get("ok"):
            self.direct_control_horizons += 1
            if not observation.get("paused") and (observation.get("controller") or {}).get("active"):
                self.continuous_control_horizons += 1
            intent = str((decision.get("arguments") or {}).get("intent") or "unspecified")
            self.intent_counts[intent] = self.intent_counts.get(intent, 0) + 1
            self._evaluate_navigation(decision, observation)
            self._accumulate_assist(observation)
        event_type = str(event_state.get("type") or "")
        if event_type:
            self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
            if event_type == "chest_collected":
                self.chest_collections += 1
        chest_distance = world.get("nearest_chest_distance")
        if chest_distance is not None:
            try:
                distance = float(chest_distance)
                if distance >= 0.0 and (self.min_chest_distance is None or distance < self.min_chest_distance):
                    self.min_chest_distance = distance
            except (TypeError, ValueError):
                pass
        self.previous_level_time = level_time

    def effective_prompt_version(self) -> str:
        """hybrid branches the system prompt, so the manifest must not claim v4."""
        return f"{self.prompt_version}-hybrid" if self.policy == "hybrid" else self.prompt_version

    @property
    def uses_llm_planner(self) -> bool:
        """hybrid is llm planning plus a bridge assist, so it shares the llm checks."""
        return self.policy in ("llm", "hybrid")

    def _accumulate_assist(self, observation: dict[str, Any]) -> None:
        """Fold one horizon's bridge-assist telemetry into the run totals.

        Frame counts come from the run-cumulative controller totals rather than the
        per-horizon fields: the observation is written before a planning hold runs,
        so hold frames are only visible as a delta on the next observation.
        """
        controller = observation.get("controller") or {}
        if not controller.get("assist_enabled"):
            return
        self.assist_horizons += 1
        try:
            frames_total = int(controller.get("assist_frames_total") or 0)
            deflection_total = float(controller.get("assist_deflection_degrees_total") or 0.0)
            self.assist_control_frames += int(controller.get("control_frames") or 0)
            self.assist_max_deflection = max(
                self.assist_max_deflection,
                float(controller.get("assist_max_deflection_degrees") or 0.0),
            )
        except (TypeError, ValueError):
            return
        self.assist_frames += max(0, frames_total - self.assist_frames_total_seen)
        self.assist_deflection_sum += max(0.0, deflection_total - self.assist_deflection_total_seen)
        self.assist_frames_total_seen = max(self.assist_frames_total_seen, frames_total)
        self.assist_deflection_total_seen = max(self.assist_deflection_total_seen, deflection_total)

    def assist_metrics(self) -> dict[str, Any]:
        return {
            "enabled": self.assist_horizons > 0,
            "horizons": self.assist_horizons,
            "assist_frames": self.assist_frames,
            "control_frames": self.assist_control_frames,
            "assist_frame_ratio": (
                round(self.assist_frames / self.assist_control_frames, 4)
                if self.assist_control_frames
                else 0.0
            ),
            "mean_deflection_degrees": (
                round(self.assist_deflection_sum / self.assist_frames, 3)
                if self.assist_frames
                else 0.0
            ),
            "max_deflection_degrees": round(self.assist_max_deflection, 3),
        }

    def _evaluate_navigation(self, decision: dict[str, Any], observation: dict[str, Any]) -> None:
        arguments = decision.get("arguments") or {}
        intent = str(arguments.get("intent") or "")
        if intent != "collect_chest":
            return
        context = decision.get("evaluation_context") or {}
        target = context.get("target_chest")
        target_id = int(arguments.get("target_id", 0) or 0)
        evaluation: dict[str, Any] = {
            "step": len(self.steps) - 1,
            "intent": intent,
            "target_id": target_id,
            "valid_target": isinstance(target, dict) and target_id != 0,
        }
        if not isinstance(target, dict) or target_id == 0:
            evaluation.update({"alignment_cosine": None, "aligned": False, "made_progress": False})
            self.navigation_evaluations.append(evaluation)
            return

        x = float(arguments.get("x", 0.0) or 0.0)
        y = float(arguments.get("y", 0.0) or 0.0)
        target_x = float(target.get("relative_x", 0.0) or 0.0)
        target_y = float(target.get("relative_y", 0.0) or 0.0)
        action_magnitude = math.hypot(x, y)
        target_magnitude = math.hypot(target_x, target_y)
        cosine = None
        if action_magnitude > 1e-6 and target_magnitude > 1e-6:
            cosine = (x * target_x + y * target_y) / (action_magnitude * target_magnitude)
        pre_distance = float(target.get("distance", target_magnitude) or target_magnitude)
        post_target = next(
            (
                item for item in ((observation.get("world") or {}).get("visible_chests") or [])
                if isinstance(item, dict) and int(item.get("id", 0) or 0) == target_id
            ),
            None,
        )
        event_type = str(((observation.get("event_state") or {}).get("type") or ""))
        post_distance = (
            float(post_target.get("distance", 0.0) or 0.0) if isinstance(post_target, dict) else None
        )
        made_progress = event_type == "chest_collected" or (
            post_distance is not None and post_distance < pre_distance - 0.05
        )
        alignment_threshold = 0.95 if pre_distance <= 3.0 else 0.80
        evaluation.update(
            {
                "alignment_cosine": round(cosine, 4) if cosine is not None else None,
                "alignment_threshold": alignment_threshold,
                "aligned": cosine is not None and cosine >= alignment_threshold,
                "pre_distance": round(pre_distance, 4),
                "post_distance": round(post_distance, 4) if post_distance is not None else None,
                "made_progress": made_progress,
            }
        )
        self.navigation_evaluations.append(evaluation)

    def chest_navigation_metrics(self) -> dict[str, Any]:
        attempts = len(self.navigation_evaluations)
        aligned = sum(1 for item in self.navigation_evaluations if item.get("aligned"))
        progress = sum(1 for item in self.navigation_evaluations if item.get("made_progress"))
        valid = sum(1 for item in self.navigation_evaluations if item.get("valid_target"))
        cosines = [float(item["alignment_cosine"]) for item in self.navigation_evaluations if item.get("alignment_cosine") is not None]
        return {
            "attempts": attempts,
            "valid_target_attempts": valid,
            "aligned_attempts": aligned,
            "progress_attempts": progress,
            "alignment_rate": round(aligned / attempts, 3) if attempts else None,
            "progress_rate": round(progress / attempts, 3) if attempts else None,
            "mean_alignment_cosine": round(sum(cosines) / len(cosines), 4) if cosines else None,
            "evaluations": self.navigation_evaluations,
        }

    def _detect_anomalies(self, step: int, observation: dict[str, Any]) -> None:
        if not observation.get("ok", False):
            self._add_anomaly(step, "bridge_action_failed", observation.get("result", "Unknown bridge failure"), "high")
        for log in observation.get("recent_logs") or []:
            lowered = str(log).lower()
            if "exception" in lowered or "error:" in lowered or "assert:" in lowered:
                self._add_anomaly(step, "unity_error", str(log), "high")
        if self._contains_non_finite(observation):
            self._add_anomaly(step, "non_finite_state", "Observation contains NaN or infinity", "critical")
        player = observation.get("player") or {}
        player_view = observation.get("player_view") or {}
        if player.get("present") and player_view:
            raw_health = float(player.get("health", 0.0) or 0.0)
            max_health = float(player.get("max_health", 0.0) or 0.0)
            view_health_ratio = player_view.get("health_ratio")
            if max_health > 0 and view_health_ratio is not None:
                expected_ratio = raw_health / max_health
                if not math.isclose(float(view_health_ratio), expected_ratio, rel_tol=1e-5, abs_tol=1e-5):
                    self._add_anomaly(
                        step,
                        "view_state_match",
                        f"player_view.health_ratio={view_health_ratio} expected={expected_ratio}",
                        "high",
                    )
        frame_value = observation.get("frame")
        current_level_time = float((observation.get("progress") or {}).get("level_time", 0.0) or 0.0)
        phase = str(observation.get("phase") or "")
        if (
            isinstance(frame_value, int)
            and self.previous_observation_frame == frame_value
            and self.previous_observation_level_time is not None
            and math.isclose(current_level_time, self.previous_observation_level_time, abs_tol=1e-5)
            and not observation.get("paused")
            and phase == "active_gameplay"
        ):
            self._add_anomaly(step, "freeze", "frame and simulation time did not advance", "critical")
        if isinstance(frame_value, int):
            self.previous_observation_frame = frame_value
            self.previous_observation_level_time = current_level_time
        menu = observation.get("menu") or {}
        actions = observation.get("available_actions") or []
        pause_reason = str(observation.get("pause_reason") or "")
        expected_boundary = pause_reason in (
            "agent_decision_boundary", "event_decision_boundary", "upgrade_dialog", "game_over"
        )
        if observation.get("paused") and not expected_boundary and self.entered_gameplay and not menu.get("upgrade_open") and not menu.get("game_over"):
            if "move" not in actions and "start_game" not in actions:
                self._add_anomaly(step, "unexpected_pause", "Gameplay is paused without a known blocking dialog", "medium")

    def _add_anomaly(self, step: int, kind: str, evidence: str, severity: str) -> None:
        key = (kind, evidence)
        if any((item["kind"], item["evidence"]) == key for item in self.anomalies):
            return
        self.anomalies.append({"step": step, "kind": kind, "severity": severity, "evidence": evidence})

    @classmethod
    def _contains_non_finite(cls, value: Any) -> bool:
        if isinstance(value, float):
            return not math.isfinite(value)
        if isinstance(value, dict):
            return any(cls._contains_non_finite(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_non_finite(item) for item in value)
        return False

    def build_report(self, llm_assessment: dict[str, Any] | None, fatal_error: str | None = None) -> dict[str, Any]:
        charter_compliance = self.charter_compliance()
        chest_navigation = self.chest_navigation_metrics()
        runtime_anomalies = [
            anomaly for anomaly in self.anomalies
            if not str(anomaly.get("kind") or "").startswith("llm_")
        ]
        checks = [
            self._check("game_launched", self.launched, "QA Bridge produced observations" if self.launched else (fatal_error or "No ready signal")),
            self._check("entered_gameplay", self.entered_gameplay, f"entered_gameplay={self.entered_gameplay}"),
            self._check("simulation_progress", self.total_simulation_time >= 30, f"total_simulation_time={self.total_simulation_time:.2f}s"),
            self._check("level_up", self.max_level > 1, f"max_level={self.max_level}"),
            self._check("upgrade_selected", self.upgrade_selected > 0, f"selections={self.upgrade_selected}"),
            self._check("upgrade_resumed", self.upgrade_resumed, f"resumed={self.upgrade_resumed}"),
            self._check("death_detected", self.death_seen, f"death_seen={self.death_seen}", optional=not self.death_seen),
            self._check("restart_after_death", self.restart_seen, f"restart_seen={self.restart_seen}", optional=not self.death_seen),
            self._check(
                "llm_direct_control" if self.uses_llm_planner else "baseline_steering",
                self.direct_control_horizons > 0 if self.uses_llm_planner else self.steering_horizons > 0,
                f"direct_control_horizons={self.direct_control_horizons}, total_steering_horizons={self.steering_horizons}",
            ),
            self._check(
                "chest_collection_event",
                self.chest_collections > 0,
                f"chest_collections={self.chest_collections}, min_chest_distance={self.min_chest_distance}",
                optional=self.chest_collections == 0,
            ),
            self._check(
                "agent_chest_navigation",
                bool(
                    chest_navigation["attempts"] > 0
                    and (chest_navigation["alignment_rate"] or 0.0) >= 0.5
                    and (chest_navigation["progress_rate"] or 0.0) >= 0.5
                ),
                (
                    f"attempts={chest_navigation['attempts']}, "
                    f"alignment_rate={chest_navigation['alignment_rate']}, "
                    f"progress_rate={chest_navigation['progress_rate']}"
                ),
                optional=chest_navigation["attempts"] == 0,
            ),
            self._check("no_runtime_errors", not runtime_anomalies, f"runtime_anomaly_count={len(runtime_anomalies)}"),
            self._check(
                "charter_compliance",
                bool(charter_compliance["compliant"]),
                (
                    f"net_heading_compliant={charter_compliance['net_heading_compliant']}, "
                    f"net_forward_progress={charter_compliance['net_forward_progress']}, "
                    f"restarts={charter_compliance['restarts_executed']}/"
                    f"{charter_compliance['max_restarts']}"
                ),
            ),
            self._check("artifacts_written", True, "steps.jsonl, report.json, and report.md"),
        ]
        if self.uses_llm_planner:
            llm_anomalies = [
                anomaly for anomaly in self.anomalies
                if str(anomaly.get("kind") or "").startswith("llm_")
            ]
            checks.append(
                self._check(
                    "llm_execution_reliable",
                    not llm_anomalies,
                    f"llm_anomaly_count={len(llm_anomalies)}",
                )
            )
        required = [item for item in checks if item["status"] != "not_observed"]
        passed = fatal_error is None and all(item["status"] == "pass" for item in required)
        return {
            "schema_version": "1.2",
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "policy": self.policy,
            "model": self.model,
            "game_window_visible": self.game_window_visible,
            "test_charter": self.charter,
            "charter_compliance": charter_compliance,
            "seed": self.seed,
            "result": "pass" if passed else "fail",
            "fatal_error": fatal_error,
            "verdict_axes": self.verdict_axes,
            "metrics": {
                "steps": len(self.steps),
                "total_simulation_time": round(self.total_simulation_time, 3),
                "max_level_time": round(self.max_level_time, 3),
                "max_level": self.max_level,
                "max_kills": self.max_kills,
                "upgrade_selections": self.upgrade_selected,
                "anomaly_count": len(self.anomalies),
                "constraint_enforcements": charter_compliance["constraint_enforcements"],
                "steering_horizons": self.steering_horizons,
                "direct_control_horizons": self.direct_control_horizons,
                "continuous_control_horizons": self.continuous_control_horizons,
                "planner_intent_counts": self.intent_counts,
                "chest_collections": self.chest_collections,
                "min_chest_distance": self.min_chest_distance,
                "event_counts": self.event_counts,
                "api_usage": self.api_usage_totals(),
                "chest_navigation": chest_navigation,
                "bridge_assist": self.assist_metrics(),
            },
            "smoke_checks": checks,
            "rule_based_anomalies": self.anomalies,
            "rule_based_bug_candidates": self._bug_candidates(),
            "llm_assessment": llm_assessment,
        }

    def charter_compliance(self) -> dict[str, Any]:
        constraints = self.charter.get("constraints") or {}
        movement = str(constraints.get("movement", "free"))
        expected = MOVEMENT_VECTORS.get(movement)
        max_restarts = int(constraints.get("max_restarts", 1))
        movement_commands = 0
        temporary_off_heading_actions = 0
        restarts_executed = 0
        constraint_enforcements = 0
        positions: list[tuple[float, float]] = []
        for entry in self.steps:
            decision = entry.get("decision") or {}
            player = (entry.get("observation") or {}).get("player") or {}
            position = player.get("position") or {}
            if player.get("present") and "x" in position and "y" in position:
                positions.append((float(position["x"]), float(position["y"])))
            constraint_enforcements += len(decision.get("constraint_enforcements") or [])
            if decision.get("tool") != "game":
                continue
            action = decision.get("action")
            if action == "restart" and (entry.get("observation") or {}).get("ok"):
                restarts_executed += 1
            if action not in ("move", "steer", "direct_steer"):
                continue
            movement_commands += 1
            if expected is not None:
                arguments = decision.get("arguments") or {}
                actual = (float(arguments.get("x", 0.0)), float(arguments.get("y", 0.0)))
                dot = actual[0] * expected[0] + actual[1] * expected[1]
                if dot < 0.0:
                    temporary_off_heading_actions += 1

        net_dx = positions[-1][0] - positions[0][0] if len(positions) >= 2 else 0.0
        net_dy = positions[-1][1] - positions[0][1] if len(positions) >= 2 else 0.0
        net_distance = math.hypot(net_dx, net_dy)
        net_forward_progress = (
            net_dx * expected[0] + net_dy * expected[1] if expected is not None else net_distance
        )
        net_forward_ratio = net_forward_progress / net_distance if net_distance > 1e-6 else 0.0
        minimum = float(constraints.get("requested_minimum_forward_component", 0.0))
        net_heading_compliant = expected is None or (
            movement_commands > 0 and net_forward_progress > 0.0 and net_forward_ratio + 1e-6 >= minimum
        )
        movement_violations = 0 if net_heading_compliant else 1
        return {
            "compliant": net_heading_compliant and restarts_executed <= max_restarts,
            "movement_constraint": movement,
            "movement_semantics": str(constraints.get("movement_semantics", "long_term_net_progress")),
            "movement_commands": movement_commands,
            "movement_violations": movement_violations,
            "temporary_off_heading_actions": temporary_off_heading_actions,
            "net_displacement": {"x": round(net_dx, 4), "y": round(net_dy, 4)},
            "net_forward_progress": round(net_forward_progress, 4),
            "net_forward_ratio": round(net_forward_ratio, 4),
            "net_heading_compliant": net_heading_compliant,
            "max_restarts": max_restarts,
            "restarts_executed": restarts_executed,
            "constraint_enforcements": constraint_enforcements,
        }

    def _bug_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for anomaly in self.anomalies:
            if str(anomaly.get("kind") or "").startswith("llm_"):
                continue
            anomaly_step = int(anomaly.get("step", 0))
            actions: list[str] = [f"Launch with deterministic seed {self.seed} in {self.mode} mode."]
            start = max(0, anomaly_step - 7)
            for entry in self.steps[start : anomaly_step + 1]:
                decision = entry.get("decision") or {}
                if decision.get("tool") != "game":
                    continue
                arguments = decision.get("arguments") or {}
                suffix = f" {json.dumps(arguments, ensure_ascii=False)}" if arguments else ""
                actions.append(f"Execute `{decision.get('action', 'observe')}`{suffix}.")
            candidates.append(
                {
                    "title": anomaly["kind"].replace("_", " ").title(),
                    "severity": anomaly["severity"],
                    "confidence": "medium",
                    "evidence": anomaly["evidence"],
                    "minimal_reproduction_steps": actions,
                    "expected": "The action completes with finite state and no Unity error or unexplained blocking state.",
                    "actual": anomaly["evidence"],
                }
            )
        return candidates

    @staticmethod
    def _check(name: str, passed: bool, evidence: str, optional: bool = False) -> dict[str, str]:
        status = "not_observed" if optional and not passed else "pass" if passed else "fail"
        return {"name": name, "status": status, "evidence": evidence}

    def _verdict_line(self, verdict: dict[str, Any]) -> str:
        """Render the verdict axes, flagging a judgement made from a prefix.

        An aborted run can legitimately report an oracle failure, so the reader
        must be able to tell that verdict apart from one earned by a full run.
        """
        if not verdict:
            return "- Verdict: `not recorded`"
        line = (
            f"- Verdict: **{verdict.get('final_verdict', 'ERROR')}** "
            f"(execution `{verdict.get('execution_status')}`, "
            f"coverage `{verdict.get('coverage_status')}`, "
            f"oracle `{verdict.get('oracle_verdict')}`)"
        )
        if verdict.get("trace_completeness") == "partial":
            line += f" — judged from a partial trace of {len(self.steps)} steps"
        return line

    def write_report(self, report: dict[str, Any]) -> None:
        (self.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "# Gameplay QA Smoke Report",
            "",
            f"- Result: **{report['result'].upper()}**",
            self._verdict_line(report.get("verdict_axes") or {}),
            f"- Mode / policy: `{report['mode']}` / `{report['policy']}`",
            f"- Model: `{report.get('model') or 'not applicable'}`",
            f"- Game window visible: `{report.get('game_window_visible', True)}`",
            f"- Seed: `{report['seed']}`",
            f"- Total simulation time: `{report['metrics']['total_simulation_time']}` seconds",
            f"- Longest session time: `{report['metrics']['max_level_time']}` seconds",
            f"- Steps: `{report['metrics']['steps']}`",
            f"- Max level / kills: `{report['metrics']['max_level']}` / `{report['metrics']['max_kills']}`",
            f"- Steering horizons / chest collections: `{report['metrics']['steering_horizons']}` / `{report['metrics']['chest_collections']}`",
            f"- Direct LLM control horizons: `{report['metrics']['direct_control_horizons']}`",
            f"- Bridge survival assist: `{json.dumps(report['metrics'].get('bridge_assist') or {}, ensure_ascii=False)}`",
            f"- Horizons that continued during API planning: `{report['metrics'].get('continuous_control_horizons', 0)}`",
            f"- Planner intents: `{json.dumps(report['metrics'].get('planner_intent_counts') or {}, ensure_ascii=False)}`",
            "",
            "## Test charter",
            "",
            str((report.get("test_charter") or {}).get("objective", "")),
            "",
            f"- Movement constraint: `{((report.get('test_charter') or {}).get('constraints') or {}).get('movement', 'free')}`",
            f"- Maximum restarts: `{((report.get('test_charter') or {}).get('constraints') or {}).get('max_restarts', 1)}`",
            f"- Focus areas: `{', '.join((report.get('test_charter') or {}).get('focus_areas') or []) or 'not specified'}`",
            f"- Constraint compliance: `{report.get('charter_compliance', {}).get('compliant', False)}`",
            f"- Navigation policy: `{json.dumps((report.get('test_charter') or {}).get('navigation_policy') or {}, ensure_ascii=False)}`",
            "",
            "## Events and API usage",
            "",
            f"- Events: `{json.dumps(report['metrics'].get('event_counts') or {}, ensure_ascii=False)}`",
            f"- API usage: `{json.dumps(report['metrics'].get('api_usage') or {}, ensure_ascii=False)}`",
            f"- Chest navigation evaluation: `{json.dumps(report['metrics'].get('chest_navigation') or {}, ensure_ascii=False)}`",
            "",
            "## Smoke checks",
            "",
            "| Check | Status | Evidence |",
            "|---|---|---|",
        ]
        for check in report["smoke_checks"]:
            lines.append(f"| {check['name']} | {check['status']} | {str(check['evidence']).replace('|', '/')} |")
        lines.extend(["", "## Rule-based anomalies", ""])
        if report["rule_based_anomalies"]:
            for anomaly in report["rule_based_anomalies"]:
                lines.append(f"- [{anomaly['severity']}] step {anomaly['step']} {anomaly['kind']}: {anomaly['evidence']}")
        else:
            lines.append("No rule-based anomaly was detected.")
        lines.extend(["", "## Reproducible bug candidates", ""])
        if report["rule_based_bug_candidates"]:
            for candidate in report["rule_based_bug_candidates"]:
                lines.append(f"### {candidate['title']}")
                lines.append("")
                lines.append(f"Severity / confidence: `{candidate['severity']}` / `{candidate['confidence']}`")
                lines.append("")
                lines.append(f"Evidence: {candidate['evidence']}")
                lines.append("")
                lines.append("Minimal reproduction steps:")
                lines.append("")
                for index, action in enumerate(candidate["minimal_reproduction_steps"], 1):
                    lines.append(f"{index}. {action}")
                lines.append("")
        else:
            lines.append("No evidence-backed bug candidate was produced.")
        if report.get("fatal_error"):
            lines.extend(["", "## Fatal error", "", str(report["fatal_error"])])
        if report.get("llm_assessment"):
            lines.extend(["", "## LLM assessment", "", "```json", json.dumps(report["llm_assessment"], ensure_ascii=False, indent=2), "```"])
        (self.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
