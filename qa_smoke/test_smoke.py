from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import bridge_client as bridge_client_module
from . import benchmark as benchmark_module
from .bridge_client import BridgeClient
from .charter import TestCharter
from .evaluation import (
    CoverageResult,
    EvaluationContractError,
    OracleResult,
    evaluate_coverage,
    evaluate_oracle,
)
from .planners import (
    HeuristicPlanner,
    LLMPlanner,
    build_action_contract,
    build_decision_response_schema,
    canonicalize_decision_arguments,
    compact_observation,
    validate_decision_against_contract,
)
from .reporting import RunRecorder
from . import run as run_module
from .run import attach_navigation_evaluation_context, normalize_decision, terminal_stop_reason
from .scenarios import Scenario, ScenarioContractError, load_scenario
from .source_tools import SourceTools


TEST_TEMP_ROOT = Path(__file__).resolve().parent / ".test_tmp"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


class BridgeClientTests(unittest.TestCase):
    class AliveProcess:
        @staticmethod
        def poll() -> None:
            return None

    def make_plain_client(
        self, name: str, run_id: str = "run-42", scenario_id: str = "scenario-7"
    ) -> BridgeClient:
        executable = TEST_TEMP_ROOT / f"{name}.exe"
        executable.touch()
        return BridgeClient(
            executable,
            TEST_TEMP_ROOT / name,
            "qa",
            42,
            1.0,
            run_id=run_id,
            scenario_id=scenario_id,
        )

    @staticmethod
    def write_ready(client: BridgeClient, **overrides: object) -> None:
        client.bridge_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "protocol_version": "1.4",
            "run_id": client.run_id,
            "scenario_id": client.scenario_id,
            "ready": True,
        }
        payload.update(overrides)
        client.ready_path.write_text(json.dumps(payload), encoding="utf-8")

    def launch_with_fake_process(self, client: BridgeClient) -> dict[str, object]:
        with patch(
            "qa_smoke.bridge_client.subprocess.Popen", return_value=self.AliveProcess()
        ):
            try:
                return client.launch()
            finally:
                if client._stdout is not None:
                    client._stdout.close()
                    client._stdout = None

    def test_macos_bundle_is_resolved_before_launch(self) -> None:
        bundle = TEST_TEMP_ROOT / "bridge-player.app"
        executable_directory = bundle / "Contents" / "MacOS"
        executable_directory.mkdir(parents=True, exist_ok=True)
        (bundle / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0"?><plist version="1.0"><dict>'
            "<key>CFBundleExecutable</key><string>BridgePlayer</string>"
            "</dict></plist>",
            encoding="utf-8",
        )
        executable = executable_directory / "BridgePlayer"
        executable.touch()

        session = TEST_TEMP_ROOT / "session"
        client = BridgeClient(
            bundle, session, "qa", 42, 1.0, run_id="run-mac", scenario_id=""
        )
        self.write_ready(client)

        with patch(
            "qa_smoke.bridge_client.subprocess.Popen", return_value=self.AliveProcess()
        ) as popen:
            try:
                client.launch()
            finally:
                if client._stdout is not None:
                    client._stdout.close()
                    client._stdout = None

        launch_arguments = popen.call_args.args[0]
        self.assertEqual(str(executable.resolve()), launch_arguments[0])
        self.assertIn("-qaRunId=run-mac", launch_arguments)
        self.assertIn("-qaScenarioId=", launch_arguments)

    def test_launch_rejects_unknown_protocol_version(self) -> None:
        client = self.make_plain_client("unknown-protocol")
        self.write_ready(client, protocol_version="9.9")

        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "protocol_version"):
            self.launch_with_fake_process(client)

    def test_launch_rejects_run_identifier_mismatch(self) -> None:
        client = self.make_plain_client("run-mismatch")
        self.write_ready(client, run_id="another-run")

        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "run_id"):
            self.launch_with_fake_process(client)


class HeuristicPlannerTests(unittest.TestCase):
    def test_enters_game_from_menu(self) -> None:
        planner = HeuristicPlanner()
        decision = planner.plan({"available_actions": ["start_game"], "menu": {}}, 0, [])
        self.assertEqual("start_game", decision["action"])

    def test_resolves_upgrade_dialog(self) -> None:
        planner = HeuristicPlanner()
        decision = planner.plan(
            {"available_actions": ["select_upgrade"], "menu": {"upgrade_open": True}}, 3, []
        )
        self.assertEqual("select_upgrade", decision["action"])

    def test_moves_away_from_nearest_enemy(self) -> None:
        planner = HeuristicPlanner()
        decision = planner.plan(
            {
                "available_actions": ["move"],
                "menu": {},
                "world": {"nearest_enemy_vector": {"x": 2.0, "y": 0.0}},
            },
            2,
            [],
        )
        self.assertLess(decision["arguments"]["x"], 0)

    def test_heuristic_prefers_hierarchical_steering(self) -> None:
        planner = HeuristicPlanner(charter=TestCharter(movement_constraint="east"))
        decision = planner.plan({"available_actions": ["steer", "move"], "menu": {}, "world": {}}, 2, [])
        self.assertEqual("steer", decision["action"])
        self.assertEqual(1.0, decision["arguments"]["x"])

    def test_game_over_stops_before_another_api_call_when_restart_budget_is_exhausted(self) -> None:
        observation = {"phase": "game_over", "menu": {"game_over": True}}
        reason = terminal_stop_reason(observation, restarts_used=1, max_restarts=1)
        self.assertIsNotNone(reason)

    def test_game_over_allows_planning_when_restart_budget_remains(self) -> None:
        observation = {"phase": "game_over", "menu": {"game_over": True}}
        self.assertIsNone(terminal_stop_reason(observation, restarts_used=0, max_restarts=1))

    def test_heading_constraint_and_local_policy_override_planner_steer(self) -> None:
        decision = normalize_decision(
            {"tool": "game", "action": "steer", "arguments": {"x": -1, "y": 0.5, "duration": 9}},
            "qa",
            TestCharter(objective="Move east only", movement_constraint="east"),
            action_seconds=2.0,
        )
        self.assertEqual(1.0, decision["arguments"]["x"])
        self.assertEqual(0.0, decision["arguments"]["y"])
        self.assertEqual(2.0, decision["arguments"]["duration"])
        self.assertTrue(decision["arguments"]["collect_chests"])
        self.assertEqual(0.15, decision["arguments"]["min_forward_component"])
        self.assertEqual("movement", decision["constraint_enforcements"][0]["constraint"])

    def test_legacy_move_preserves_lateral_control_with_forward_floor(self) -> None:
        decision = normalize_decision(
            {"tool": "game", "action": "move", "arguments": {"x": -1, "y": 1, "duration": 1}},
            "qa",
            TestCharter(movement_constraint="east", min_forward_component=0.2),
        )
        self.assertGreaterEqual(decision["arguments"]["x"], 0.2 - 1e-6)
        self.assertGreater(decision["arguments"]["y"], 0.0)

    def test_llm_direct_control_does_not_correct_direction(self) -> None:
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": -1, "y": 0.25, "duration": 9, "intent": "evade", "target_id": 42},
            },
            "qa",
            TestCharter(movement_constraint="east"),
            action_seconds=2.0,
            llm_direct_control=True,
        )
        self.assertEqual(-1.0, decision["arguments"]["x"])
        self.assertEqual(0.25, decision["arguments"]["y"])
        self.assertEqual(2.0, decision["arguments"]["duration"])
        self.assertEqual("evade", decision["arguments"]["intent"])
        self.assertEqual(42, decision["arguments"]["target_id"])
        self.assertTrue(decision["arguments"]["continue_during_planning"])
        self.assertFalse(any(item.get("constraint") == "movement" for item in decision["constraint_enforcements"]))

    def test_restart_budget_is_enforced(self) -> None:
        decision = normalize_decision(
            {"tool": "game", "action": "restart", "arguments": {}},
            "qa",
            TestCharter(objective="Never restart", max_restarts=0),
            restarts_used=0,
        )
        self.assertEqual("observe", decision["action"])


class LLMPlannerTests(unittest.TestCase):
    def test_user_charter_is_sent_in_every_plan_request(self) -> None:
        charter = TestCharter(
            objective="Survive and probe the east boundary",
            movement_constraint="east",
            focus_areas=("world_boundary",),
        )
        planner = LLMPlanner("qa", "test-model", charter, 2.0, api_key="test-key")
        response = {"plan": "test", "hypothesis": "", "tool": "game", "action": "observe", "arguments": {}}
        with patch.object(planner, "_request", return_value=response) as request:
            planner.plan({"available_actions": ["observe"]}, 3, [])
        payload = json.loads(request.call_args.args[1])
        self.assertEqual("Survive and probe the east boundary", payload["test_charter"]["objective"])
        self.assertEqual("east", payload["test_charter"]["constraints"]["movement"])
        system_prompt = request.call_args.args[0]
        self.assertIn("does NOT automatically avoid enemies", system_prompt)
        self.assertIn("direct_steer", system_prompt)
        self.assertIn("long-term NET-PROGRESS", system_prompt)

    def test_active_gameplay_contract_rejects_unpause_loop_without_choosing_vector(self) -> None:
        observation = {
            "paused": True,
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["observe", "direct_steer", "wait"],
        }
        charter = TestCharter(movement_constraint="east")
        contract = build_action_contract(observation, "qa", charter, 6)
        self.assertEqual("active_gameplay", contract["phase"])
        self.assertEqual(["game.direct_steer"], contract["allowed_calls"])
        self.assertIsNotNone(validate_decision_against_contract(
            {"tool": "game", "action": "wait", "arguments": {"duration": 1}}, contract
        ))
        self.assertIsNotNone(validate_decision_against_contract(
            {"tool": "game", "action": "start_game", "arguments": {"index": 0}}, contract
        ))
        self.assertIsNone(validate_decision_against_contract(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 0.7, "y": 0.7, "duration": 5},
            },
            contract,
        ))

    def test_compact_observation_explains_bridge_decision_boundary(self) -> None:
        compact = compact_observation({
            "paused": True,
            "time_scale": 0.0,
            "player": {"present": True, "alive": True},
            "menu": {},
            "world": {},
            "available_actions": ["direct_steer", "wait"],
        })
        self.assertEqual("active_gameplay", compact["phase"])
        self.assertEqual("agent_decision_boundary", compact["bridge_clock"]["pause_reason"])
        self.assertTrue(compact["bridge_clock"]["agent_decision_boundary"])
        self.assertNotIn("paused", compact)

    def test_plan_payload_contains_phase_specific_action_contract(self) -> None:
        planner = LLMPlanner(
            "qa", "test-model", TestCharter(movement_constraint="east"), 5.0, api_key="test-key"
        )
        response = {
            "plan": "Move diagonally toward chest.",
            "hypothesis": "",
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 0.8, "y": 0.2, "duration": 5},
        }
        observation = {
            "paused": True,
            "player": {"present": True, "alive": True},
            "menu": {},
            "world": {},
            "available_actions": ["direct_steer", "wait"],
        }
        with patch.object(planner, "_request", return_value=response) as request:
            planner.plan(observation, 1, [], source_steps_remaining=4)
        payload = json.loads(request.call_args.args[1])
        self.assertEqual(["game.direct_steer"], payload["action_contract"]["allowed_calls"])
        self.assertIn("Never use start_game or wait", request.call_args.args[0])
        response_schema = request.call_args.kwargs["response_schema"]
        self.assertEqual(["direct_steer"], response_schema["properties"]["action"]["enum"])

    def test_upgrade_contract_exposes_indices_and_requires_model_choice(self) -> None:
        observation = {
            "phase": "upgrade_selection",
            "menu": {
                "upgrade_open": True,
                "choices": [
                    {"index": 0, "name": "Grenade"},
                    {"index": 1, "name": "Armor+"},
                    {"index": 2, "name": "Dagger"},
                ],
            },
            "available_actions": ["select_upgrade"],
        }
        contract = build_action_contract(observation, "qa", TestCharter(), 6)
        self.assertEqual([0, 1, 2], contract["allowed_indices"])
        self.assertIsNone(validate_decision_against_contract(
            {"tool": "game", "action": "select_upgrade", "arguments": {"index": 2}},
            contract,
        ))
        self.assertIsNotNone(validate_decision_against_contract(
            {"tool": "game", "action": "select_upgrade", "arguments": {"index": 7}},
            contract,
        ))
        schema = build_decision_response_schema(contract)
        self.assertIsNotNone(schema)
        index_schema = schema["properties"]["arguments"]["properties"]["index"]
        self.assertEqual([0, 1, 2], index_schema["enum"])

    def test_official_gpt4o_mini_uses_strict_structured_output(self) -> None:
        planner = LLMPlanner("qa", "gpt-4o-mini", TestCharter(), 5.0, api_key="test-key")
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        response_format = planner._response_format(schema)
        self.assertEqual("json_schema", response_format["type"])
        self.assertTrue(response_format["json_schema"]["strict"])
        compatible = LLMPlanner(
            "qa", "some-provider-model", TestCharter(), 5.0,
            api_url="https://provider.example/v1/chat/completions", api_key="test-key",
        )
        self.assertEqual({"type": "json_object"}, compatible._response_format(schema))

    def test_argument_syntax_normalization_preserves_llm_selected_upgrade(self) -> None:
        encoded = canonicalize_decision_arguments({
            "tool": "game", "action": "select_upgrade", "arguments": "{\"index\": 2}"
        })
        self.assertEqual({"index": 2}, encoded["arguments"])
        self.assertIn("decoded_json_string", encoded["_syntax_normalizations"])
        array_wrapped = canonicalize_decision_arguments({
            "tool": "game", "action": "select_upgrade", "arguments": [1]
        })
        self.assertEqual({"index": 1}, array_wrapped["arguments"])
        self.assertIn("unwrapped_single_item_array", array_wrapped["_syntax_normalizations"])

    def test_observation_is_compacted_to_nearest_threats_without_mutating_raw_state(self) -> None:
        raw_entities = [
            {"id": index, "distance": float(20 - index), "relative_x": index, "relative_y": 0}
            for index in range(20)
        ]
        observation = {
            "world": {"qa_entities": raw_entities, "visible_chests": []},
            "player": {}, "progress": {}, "menu": {}, "inventory": {}, "controller": {},
        }
        compact = compact_observation(observation)
        threats = compact["world"]["threat_entities"]
        self.assertEqual(8, len(threats))
        self.assertEqual(19, threats[0]["id"])
        self.assertNotIn("qa_entities", compact["world"])
        self.assertEqual(20, len(observation["world"]["qa_entities"]))

    def test_usage_is_normalized_across_api_shapes(self) -> None:
        usage = LLMPlanner._normalize_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        )
        self.assertEqual(120, usage["total_tokens"])
        self.assertEqual(60, usage["cached_tokens"])
        self.assertEqual(40, usage["cache_write_tokens"])
        self.assertEqual(5, usage["reasoning_tokens"])


class ReportingTests(unittest.TestCase):
    def test_step_records_correlation_identifiers(self) -> None:
        output = TEST_TEMP_ROOT / "correlated-report"
        recorder = RunRecorder(
            output,
            "qa",
            "heuristic",
            42,
            run_id="run-42",
        )
        recorder.record(
            3,
            {
                "decision_id": "run-42-decision-00000003",
                "tool": "game",
                "action": "observe",
                "arguments": {},
            },
            {
                "run_id": "run-42",
                "observation_id": "run-42-obs-00000004",
                "player": {},
                "progress": {},
                "menu": {},
                "world": {},
                "event_state": {},
            },
            0.1,
        )

        entry = recorder.steps[0]
        self.assertEqual("run-42", entry["run_id"])
        self.assertEqual("run-42-obs-00000004", entry["observation_id"])
        self.assertEqual("run-42-decision-00000003", entry["decision_id"])

    def test_decision_identity_is_stable_for_a_run_step(self) -> None:
        decision = {"tool": "game", "action": "observe", "arguments": {}}

        identified = run_module.attach_decision_identity(decision, "run-42", 3)

        self.assertEqual("run-42-decision-00000003", identified["decision_id"])

    def test_error_log_becomes_reproducible_candidate(self) -> None:
        output = TEST_TEMP_ROOT / "report"
        output.mkdir(exist_ok=True)
        recorder = RunRecorder(
            output,
            "qa",
            "heuristic",
            7,
            charter=TestCharter(objective="Exercise report", movement_constraint="east").as_dict(),
        )
        recorder.launched = True
        recorder.record(
            0,
            {"tool": "game", "action": "observe", "arguments": {}},
            {
                "ok": True,
                "recent_logs": ["Exception: sample"],
                "player": {},
                "progress": {},
                "menu": {},
                "available_actions": [],
            },
            0.1,
        )
        report = recorder.build_report(None)
        self.assertEqual(1, len(report["rule_based_bug_candidates"]))
        self.assertIn("Launch with deterministic seed", report["rule_based_bug_candidates"][0]["minimal_reproduction_steps"][0])
        self.assertEqual("Exercise report", report["test_charter"]["objective"])

    def test_event_and_token_usage_are_reported(self) -> None:
        output = TEST_TEMP_ROOT / "event-report"
        output.mkdir(exist_ok=True)
        recorder = RunRecorder(output, "qa", "llm", 11, charter=TestCharter().as_dict(), model="mock")
        recorder.record(
            1,
            {"tool": "game", "action": "direct_steer", "arguments": {"x": 1.0, "y": 0.0, "intent": "collect_chest"}},
            {
                "ok": True,
                "player": {"present": True, "level": 1},
                "progress": {"level_time": 2.0},
                "menu": {},
                "world": {"nearest_chest_distance": 3.0},
                "event_state": {"type": "chest_collected", "detail": "count changed"},
                "available_actions": ["steer"],
            },
            0.2,
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "request_count": 2},
        )
        report = recorder.build_report(None)
        self.assertEqual(1, report["metrics"]["chest_collections"])
        self.assertEqual(120, report["metrics"]["api_usage"]["total_tokens"])
        self.assertEqual(1, report["metrics"]["direct_control_horizons"])
        self.assertEqual(2, report["metrics"]["api_usage"]["calls"])

    def test_llm_contract_failure_is_not_reported_as_a_game_bug(self) -> None:
        output = TEST_TEMP_ROOT / "agent-failure-report"
        output.mkdir(exist_ok=True)
        recorder = RunRecorder(output, "qa", "llm", 19, charter=TestCharter().as_dict(), model="mock")
        recorder.launched = True
        recorder.anomalies.append({
            "step": 0,
            "kind": "llm_action_contract_failure",
            "severity": "high",
            "evidence": "arguments must be a JSON object",
        })
        report = recorder.build_report(None, "RuntimeError: invalid model action")
        self.assertEqual([], report["rule_based_bug_candidates"])
        checks = {item["name"]: item for item in report["smoke_checks"]}
        self.assertEqual("pass", checks["no_runtime_errors"]["status"])
        self.assertEqual("fail", checks["llm_execution_reliable"]["status"])

    def test_chest_navigation_scores_selected_vector_and_distance_progress(self) -> None:
        output = TEST_TEMP_ROOT / "chest-navigation-report"
        output.mkdir(exist_ok=True)
        recorder = RunRecorder(output, "qa", "llm", 13, charter=TestCharter().as_dict(), model="mock")
        pre = {
            "player": {"present": True, "position": {"x": 0.0, "y": 0.0}},
            "progress": {"level_time": 0.0},
            "world": {
                "danger_score": 0.1,
                "escape_vector": {"x": -1.0, "y": 0.0},
                "visible_chests": [
                    {"id": 7, "relative_x": 3.0, "relative_y": 4.0, "distance": 5.0}
                ],
            },
        }
        decision = normalize_decision(
            {"tool": "game", "action": "direct_steer", "arguments": {
                "x": 0.6, "y": 0.8, "duration": 5, "intent": "collect_chest", "target_id": 7,
            }},
            "qa", TestCharter(), action_seconds=5, llm_direct_control=True,
        )
        attach_navigation_evaluation_context(decision, pre)
        recorder.record(
            0,
            decision,
            {
                "ok": True,
                "paused": False,
                "controller": {"active": True},
                "player": {"present": True, "level": 1, "position": {"x": 1.0, "y": 1.0}},
                "progress": {"level_time": 2.0},
                "menu": {},
                "world": {"visible_chests": [{"id": 7, "distance": 3.5}], "nearest_chest_distance": 3.5},
                "event_state": {},
                "available_actions": ["direct_steer"],
            },
            0.2,
        )
        metrics = recorder.chest_navigation_metrics()
        self.assertEqual(1.0, metrics["alignment_rate"])
        self.assertEqual(1.0, metrics["progress_rate"])
        self.assertEqual(1, recorder.continuous_control_horizons)

    def test_net_heading_allows_temporary_backtracking(self) -> None:
        output = TEST_TEMP_ROOT / "net-heading-report"
        output.mkdir(exist_ok=True)
        recorder = RunRecorder(
            output, "qa", "llm", 17,
            charter=TestCharter(movement_constraint="east", min_forward_component=0.1).as_dict(),
        )
        for step, (x, position_x) in enumerate(((1.0, 1.0), (-1.0, 0.5), (1.0, 3.0))):
            recorder.record(
                step,
                {"tool": "game", "action": "direct_steer", "arguments": {"x": x, "y": 0.0, "intent": "evade"}},
                {
                    "ok": True, "player": {"present": True, "level": 1, "position": {"x": position_x, "y": 0.0}},
                    "progress": {"level_time": float(step + 1)}, "menu": {}, "world": {}, "event_state": {},
                    "available_actions": ["direct_steer"],
                },
                float(step + 1),
            )
        compliance = recorder.charter_compliance()
        self.assertTrue(compliance["net_heading_compliant"])
        self.assertEqual(1, compliance["temporary_off_heading_actions"])


class SourceToolTests(unittest.TestCase):
    def test_read_rejects_path_escape(self) -> None:
        root = TEST_TEMP_ROOT / "source"
        (root / "Assets").mkdir(parents=True, exist_ok=True)
        tools = SourceTools(root)
        result = tools.read("../outside.cs")
        self.assertFalse(result["ok"])


class BridgeProtocolTests(unittest.TestCase):
    class AliveProcess:
        @staticmethod
        def poll() -> None:
            return None

    def make_client(self, name: str) -> BridgeClient:
        session = TEST_TEMP_ROOT / name
        client = BridgeClient(
            Path("unused.exe"),
            session,
            "player",
            7,
            1.0,
            run_id=f"run-{name}",
            scenario_id="",
        )
        client.response_directory.mkdir(parents=True, exist_ok=True)
        client.process = self.AliveProcess()  # type: ignore[assignment]
        return client

    def start_response(
        self,
        client: BridgeClient,
        observation_id: str,
        event_state: dict[str, object] | None = None,
    ) -> threading.Thread:
        if client.command_path.exists():
            client.command_path.unlink()

        def responder() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    command = json.loads(client.command_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.01)
                    continue
                client._write_json_atomic(
                    client.response_path(command["id"]),
                    {
                        "protocol_version": "1.4",
                        "run_id": client.run_id,
                        "scenario_id": client.scenario_id,
                        "observation_id": observation_id,
                        "command_id": command["id"],
                        "event_state": event_state or {},
                        "ok": True,
                        "result": "fake Unity response",
                    },
                )
                return

        thread = threading.Thread(target=responder)
        thread.start()
        return thread

    def test_command_waits_for_matching_atomic_response(self) -> None:
        client = self.make_client("bridge")
        thread = self.start_response(client, "obs-1")
        observation = client.command("observe", timeout=2.0)
        thread.join(timeout=2.0)
        self.assertEqual("fake Unity response", observation["result"])

    def test_duplicate_observation_identifier_is_rejected(self) -> None:
        client = self.make_client("duplicate-observation")
        first = self.start_response(client, "obs-reused")
        client.command("observe", timeout=2.0)
        first.join(timeout=2.0)

        second = self.start_response(client, "obs-reused")
        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "duplicate"):
            client.command("observe", timeout=2.0)
        second.join(timeout=2.0)

    def test_event_referencing_an_unknown_command_is_rejected(self) -> None:
        client = self.make_client("orphan-event")
        thread = self.start_response(
            client,
            "obs-event",
            {
                "event_id": "event-1",
                "caused_by_command_id": "never-issued",
                "type": "chest_collected",
            },
        )

        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "orphan"):
            client.command("observe", timeout=2.0)
        thread.join(timeout=2.0)


class ScenarioContractTests(unittest.TestCase):
    def test_missing_scenario_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScenarioContractError, "unknown scenario"):
            load_scenario("does-not-exist")

    def test_unknown_scenario_key_is_rejected(self) -> None:
        payload = {
            "id": "invalid-extra-key",
            "difficulty": "easy",
            "preset": "smoke",
            "charter": {"objective": "Exercise observation validation."},
            "seed_set": [9101, 9102, 9103],
            "limits": {"max_simulation_seconds": 60.0, "max_steps": 20},
            "coverage_target": "observe_player_state",
            "oracle": "health_ratio_consistency",
            "ground_truth": {
                "bug_id": "health_ratio_out_of_range",
                "fault_id": None,
                "expected_behavior": "Reported health values remain internally consistent.",
                "reproduction_steps": ["Observe player health."],
                "review_status": "pending",
            },
            "unexpected": True,
        }

        with self.assertRaisesRegex(Exception, "Extra inputs are not permitted"):
            Scenario.model_validate(payload)

    def test_unregistered_oracle_identifier_is_rejected(self) -> None:
        payload = {
            "id": "invalid-oracle",
            "difficulty": "easy",
            "preset": "smoke",
            "charter": {"objective": "Exercise oracle validation."},
            "seed_set": [9101, 9102, 9103],
            "limits": {"max_simulation_seconds": 60.0, "max_steps": 20},
            "coverage_target": "observe_player_state",
            "oracle": "arbitrary_python_expression",
            "ground_truth": {
                "bug_id": None,
                "fault_id": None,
                "expected_behavior": "The run remains valid.",
                "reproduction_steps": ["Observe gameplay."],
                "review_status": "pending",
            },
        }

        with self.assertRaisesRegex(Exception, "unregistered oracle"):
            Scenario.model_validate(payload)

    def test_scenario_rejects_charter_override(self) -> None:
        with self.assertRaisesRegex(ScenarioContractError, "--objective"):
            run_module.parse_args(
                [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--scenario", "easy-health-ratio",
                    "--objective", "Override the benchmark contract",
                ]
            )

    def test_scenario_allows_seed_from_owned_seed_set(self) -> None:
        args = run_module.parse_args(
            [
                "--game-exe", "player.app",
                "--output", "artifacts",
                "--scenario", "easy-health-ratio",
                "--seed", "9102",
            ]
        )

        self.assertEqual(9102, args.seed)
        self.assertEqual("easy-health-ratio", args.scenario_definition.id)
        self.assertEqual("Exercise single-observation health consistency.", args.objective)

    def test_scenario_rejects_seed_outside_owned_seed_set(self) -> None:
        with self.assertRaisesRegex(ScenarioContractError, "seed_set"):
            run_module.parse_args(
                [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--scenario", "easy-health-ratio",
                    "--seed", "1337",
                ]
            )

    def test_ad_hoc_arguments_preserve_previous_defaults_and_environment_fallbacks(self) -> None:
        with patch.dict(
            os.environ,
            {"QA_MODEL": "operator-model", "QA_API_URL": "https://operator.example/v1"},
            clear=False,
        ):
            args = run_module.parse_args(
                ["--game-exe", "player.app", "--output", "artifacts"]
            )

        charter = run_module.charter_from_args(args)
        self.assertEqual(TestCharter().as_dict(), charter.as_dict())
        self.assertEqual(1337, args.seed)
        self.assertEqual(60.0, args.max_simulation_seconds)
        self.assertEqual("operator-model", args.model)
        self.assertEqual("https://operator.example/v1", args.api_url)

    def test_benchmark_assigns_an_independent_directory_to_each_seed(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        output_root = TEST_TEMP_ROOT / "benchmark-runs"

        with patch.object(benchmark_module, "run_session", side_effect=[0, 0]) as run_session:
            results = benchmark_module.run_benchmark(
                [scenario],
                [9101, 9102],
                game_exe=Path("player.app"),
                project_root=Path.cwd(),
                output_root=output_root,
                headless=True,
                quiet=True,
            )

        self.assertEqual(
            [
                output_root / "easy-health-ratio" / "9101",
                output_root / "easy-health-ratio" / "9102",
            ],
            [result.output_dir for result in results],
        )
        first_args = run_session.call_args_list[0].args[0]
        self.assertEqual("easy-health-ratio", first_args.scenario)
        self.assertEqual("smoke", first_args.preset)
        self.assertTrue(first_args.headless)


class EvaluationTests(unittest.TestCase):
    @staticmethod
    def transition(
        observation_id: str,
        *,
        action: str = "observe",
        player: dict[str, object] | None = None,
        event_state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "decision": {"action": action},
            "observation": {
                "observation_id": observation_id,
                "player": player or {},
                "world": {},
                "progress": {},
                "inventory": {},
                "event_state": event_state or {},
            },
        }

    def test_evaluation_rejects_an_unregistered_identifier(self) -> None:
        scenario = load_scenario("easy-health-ratio").model_copy(
            update={"oracle": "unregistered-oracle"}
        )

        with self.assertRaisesRegex(EvaluationContractError, "unregistered oracle"):
            evaluate_oracle(
                scenario,
                [self.transition("obs-1", player={"present": True})],
            )

    def test_not_reached_coverage_forces_not_evaluated_oracle(self) -> None:
        scenario = load_scenario("medium-upgrade-effect")
        transitions = [self.transition("obs-1", player={"present": True})]

        coverage = evaluate_coverage(scenario, transitions)
        oracle = evaluate_oracle(scenario, transitions)

        self.assertEqual("not_reached", coverage.status)
        self.assertEqual("not_evaluated", oracle.verdict)
        self.assertEqual(["obs-1"], oracle.evidence_refs)

    def test_evaluation_evidence_references_resolve_to_transition_ids(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        transitions = [
            self.transition(
                "obs-health",
                player={
                    "present": True,
                    "health": 50.0,
                    "max_health": 100.0,
                    "health_ratio": 0.5,
                },
            )
        ]

        coverage = evaluate_coverage(scenario, transitions)
        oracle = evaluate_oracle(scenario, transitions)

        self.assertEqual("reached", coverage.status)
        self.assertEqual("pass", oracle.verdict)
        self.assertEqual(["obs-health"], coverage.evidence_refs)
        self.assertEqual(["obs-health"], oracle.evidence_refs)

    def test_verdict_models_reject_missing_evidence(self) -> None:
        with self.assertRaisesRegex(Exception, "evidence_refs"):
            CoverageResult(status="reached", evidence_refs=[])
        with self.assertRaisesRegex(Exception, "evidence_refs"):
            OracleResult(verdict="pass", evidence_refs=[])


if __name__ == "__main__":
    unittest.main()
