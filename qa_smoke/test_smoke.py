from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from . import bridge_client as bridge_client_module
from . import planners as planners_module
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
from .hypotheses import HypothesisTracker
from .memory import SessionMemory
from .planners import (
    HeuristicPlanner,
    LLMPlanner,
    build_action_contract,
    build_decision_response_schema,
    canonicalize_decision_arguments,
    compact_observation,
    compute_observed_delta,
    validate_decision_against_contract,
)
from .reporting import RunRecorder, aggregate_annotations, build_run_verdict
from . import run as run_module
from .run import (
    attach_navigation_evaluation_context,
    execute_game_action,
    normalize_decision,
    terminal_stop_reason,
)
from .scenarios import Scenario, ScenarioContractError, load_scenario, scenario_fingerprint
from .source_tools import SourceTools


TEST_TEMP_ROOT = Path(__file__).resolve().parent / ".test_tmp"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


class BridgeClientTests(unittest.TestCase):
    class AliveProcess:
        """Fake player process that becomes ready only after a boot delay.

        on_ready fires on the second poll so launch() completes one full read of
        ready.json while the player is still booting -- the window in which a
        leftover handshake file from a previous run would be picked up.
        """

        def __init__(self, on_ready: Callable[[], None] | None = None) -> None:
            self._on_ready = on_ready
            self._booting = True

        def poll(self) -> None:
            if self._booting:
                self._booting = False
            elif self._on_ready is not None:
                self._on_ready()
                self._on_ready = None

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

    def spawn_writing_ready(self, client: BridgeClient, **overrides: object) -> Callable[..., object]:
        """Fake Popen whose player writes ready.json only once it has booted.

        launch() resets bridge/ before spawning, so the handshake file has to appear
        after the reset rather than being pre-written by the test.
        """
        payload = {
            "protocol_version": "1.5",
            "run_id": client.run_id,
            "scenario_id": client.scenario_id,
            "ready": True,
        }
        payload.update(overrides)

        def write_ready() -> None:
            client.bridge_dir.mkdir(parents=True, exist_ok=True)
            client.ready_path.write_text(json.dumps(payload), encoding="utf-8")

        def spawn(*_args: object, **_kwargs: object) -> object:
            return self.AliveProcess(write_ready)

        return spawn

    def launch_with_fake_process(
        self, client: BridgeClient, **ready_overrides: object
    ) -> dict[str, object]:
        with patch(
            "qa_smoke.bridge_client.subprocess.Popen",
            side_effect=self.spawn_writing_ready(client, **ready_overrides),
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
            bundle,
            session,
            "qa",
            42,
            1.0,
            run_id="run-mac",
            scenario_id="easy-health-ratio",
            fault_id="health_ratio_out_of_range",
        )
        with patch(
            "qa_smoke.bridge_client.subprocess.Popen",
            side_effect=self.spawn_writing_ready(client),
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
        self.assertIn("-qaScenarioId=easy-health-ratio", launch_arguments)
        self.assertIn("-qaFault=health_ratio_out_of_range", launch_arguments)

    def test_launch_rejects_unknown_protocol_version(self) -> None:
        client = self.make_plain_client("unknown-protocol")

        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "protocol_version"):
            self.launch_with_fake_process(client, protocol_version="9.9")

    def test_launch_rejects_run_identifier_mismatch(self) -> None:
        client = self.make_plain_client("run-mismatch")

        with self.assertRaisesRegex(bridge_client_module.BridgeContractError, "run_id"):
            self.launch_with_fake_process(client, run_id="another-run")

    def test_launch_clears_stale_bridge_state(self) -> None:
        process_states = (
            ("exited", subprocess.CompletedProcess([], 1, stdout="", stderr="")),
            (
                "reused",
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="/usr/bin/unrelated-process\n",
                    stderr="",
                ),
            ),
        )
        for name, inspected_process in process_states:
            client = self.make_plain_client(f"stale-bridge-{name}", run_id="fresh-run")
            client.response_directory.mkdir(parents=True, exist_ok=True)
            session_report = client.session_dir / "report.json"
            session_report.write_text('{"result": "preserve"}', encoding="utf-8")
            stale_response = client.response_path("1787103218194-9d71861b")
            stale_response.write_text("{}", encoding="utf-8")
            client.command_path.write_text(
                '{"id": "old", "action": "shutdown"}', encoding="utf-8"
            )
            client.event_log_path.write_text(
                '{"run_id": "previous-run"}\n', encoding="utf-8"
            )
            client.ready_path.write_text(
                json.dumps(
                    {
                        "protocol_version": "1.5",
                        "run_id": "previous-run",
                        "scenario_id": client.scenario_id,
                        "ready": True,
                        "process_id": 4242,
                    }
                ),
                encoding="utf-8",
            )

            with self.subTest(process_state=name), patch(
                "qa_smoke.bridge_client.subprocess.run",
                return_value=inspected_process,
            ):
                ready = self.launch_with_fake_process(client)

            self.assertEqual("fresh-run", ready["run_id"])
            self.assertFalse(stale_response.exists())
            self.assertFalse(client.command_path.exists())
            self.assertFalse(client.event_log_path.exists())
            self.assertEqual(
                '{"result": "preserve"}', session_report.read_text(encoding="utf-8")
            )

    def test_launch_rejects_a_stale_player_using_the_same_executable(self) -> None:
        client = self.make_plain_client("stale-player", run_id="fresh-run")
        client.bridge_dir.mkdir(parents=True, exist_ok=True)
        client.ready_path.write_text(
            json.dumps(
                {
                    "protocol_version": "1.5",
                    "run_id": "previous-run",
                    "scenario_id": client.scenario_id,
                    "ready": True,
                    "process_id": 4242,
                }
            ),
            encoding="utf-8",
        )
        inspected_process = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"/tmp/{client.game_exe.name}\n",
            stderr="",
        )

        with patch(
            "qa_smoke.bridge_client.subprocess.run",
            return_value=inspected_process,
        ), self.assertRaisesRegex(
            bridge_client_module.BridgeError,
            "stale QA player process 4242",
        ):
            self.launch_with_fake_process(client)

    def test_launch_rejects_an_uninspectable_stale_process(self) -> None:
        client = self.make_plain_client("uninspectable-player", run_id="fresh-run")
        client.bridge_dir.mkdir(parents=True, exist_ok=True)
        client.ready_path.write_text(
            json.dumps(
                {
                    "protocol_version": "1.5",
                    "run_id": "previous-run",
                    "scenario_id": client.scenario_id,
                    "ready": True,
                    "process_id": 4242,
                }
            ),
            encoding="utf-8",
        )

        with patch(
            "qa_smoke.bridge_client.subprocess.run",
            side_effect=PermissionError("process inspection denied"),
        ), self.assertRaisesRegex(
            bridge_client_module.BridgeError,
            "could not inspect stale QA player process 4242",
        ):
            self.launch_with_fake_process(client)

    def test_launch_reports_a_bridge_cleanup_failure(self) -> None:
        client = self.make_plain_client("cleanup-failure", run_id="fresh-run")
        client.response_directory.mkdir(parents=True, exist_ok=True)

        with patch(
            "qa_smoke.bridge_client.shutil.rmtree",
            side_effect=PermissionError("cleanup denied"),
        ), self.assertRaisesRegex(
            bridge_client_module.BridgeError,
            "could not reset stale QA bridge state",
        ):
            self.launch_with_fake_process(client)

    def test_launch_clears_a_non_object_stale_ready_file(self) -> None:
        client = self.make_plain_client("non-object-ready", run_id="fresh-run")
        client.bridge_dir.mkdir(parents=True, exist_ok=True)
        client.ready_path.write_text('[{"process_id": 4242}]', encoding="utf-8")

        ready = self.launch_with_fake_process(client)

        self.assertEqual("fresh-run", ready["run_id"])


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
    def test_reflection_contract_extracts_flattened_transition_ids(self) -> None:
        contract = planners_module.build_reflection_contract(
            {
                "observation_id": "obs-2",
                "event_state": {"event_id": "event-2"},
            }
        )

        self.assertEqual(
            ["obs-2", "event-2"],
            contract["allowed_evidence_refs"],
        )

    def test_reflection_contract_extracts_nested_transition_ids(self) -> None:
        contract = planners_module.build_reflection_contract(
            {
                "observation": {
                    "observation_id": "obs-3",
                    "event_state": {"event_id": "event-3"},
                }
            }
        )

        self.assertEqual(
            ["obs-3", "event-3"],
            contract["allowed_evidence_refs"],
        )

    def test_uncertain_reflection_without_candidate_id_is_allowed(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "required_arguments": {},
            "has_previous_transition": True,
            "reflection_contract": {
                "has_previous_transition": True,
                "allowed_statuses": ["matched", "unexpected", "uncertain"],
                "evidence_refs_required": True,
                "candidate_id_required_for": ["unexpected"],
                "allowed_evidence_refs": ["obs-2"],
            },
        }
        decision = {
            "qa_observation": "The result is uncertain because nearby threats may affect health.",
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 2.0},
            "expected_effect": "The player should continue moving toward the target.",
            "reflection": {
                "status": "uncertain",
                "summary": "The outcome is not confirmed yet.",
                "evidence_refs": ["obs-2"],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        self.assertIsNone(
            validate_decision_against_contract(decision, contract)
        )

    def test_unexpected_reflection_requires_candidate_id(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "required_arguments": {},
            "has_previous_transition": True,
            "reflection_contract": {
                "has_previous_transition": True,
                "allowed_statuses": ["matched", "unexpected", "uncertain"],
                "evidence_refs_required": True,
                "candidate_id_required_for": ["unexpected"],
                "allowed_evidence_refs": ["obs-2"],
            },
        }
        decision = {
            "qa_observation": "The observed result was unexpected.",
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 2.0},
            "expected_effect": "The player should continue moving toward the target.",
            "reflection": {
                "status": "unexpected",
                "summary": "The observed result did not match the expectation.",
                "evidence_refs": ["obs-2"],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        self.assertEqual(
            "unexpected reflection requires candidate_id",
            validate_decision_against_contract(decision, contract),
        )

    def test_reflection_rejects_evidence_outside_previous_transition(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "required_arguments": {},
            "has_previous_transition": True,
            "reflection_contract": {
                "has_previous_transition": True,
                "allowed_statuses": ["matched", "unexpected", "uncertain"],
                "evidence_refs_required": True,
                "candidate_id_required_for": ["unexpected"],
                "allowed_evidence_refs": ["obs-2", "event-2"],
            },
        }
        decision = {
            "qa_observation": "The observed result matched the expectation.",
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 2.0},
            "expected_effect": "The player should continue moving toward the target.",
            "reflection": {
                "status": "matched",
                "summary": "The observed result matched the expectation.",
                "evidence_refs": ["obs-other"],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        error = validate_decision_against_contract(decision, contract)

        self.assertIn("allowed", error or "")

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
        messages = request.call_args.kwargs["messages"]
        charter_message = next(
            json.loads(message["content"])
            for message in messages
            if message["role"] == "system" and "test_charter" in message["content"]
        )
        self.assertEqual("Survive and probe the east boundary", charter_message["test_charter"]["objective"])
        self.assertEqual("east", charter_message["test_charter"]["constraints"]["movement"])
        system_prompt = request.call_args.args[0]
        self.assertIn("does NOT automatically avoid enemies", system_prompt)
        self.assertIn("direct_steer", system_prompt)
        self.assertIn("long-term NET-PROGRESS", system_prompt)

    def test_planning_payload_contains_reflection_contract_and_transition_ids(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")

        payload = planner._planning_payload(
            {
                "paused": True,
                "player": {"present": True, "alive": True},
                "menu": {},
                "available_actions": ["direct_steer"],
            },
            2,
            [
                {
                    "observation_id": "obs-2",
                    "event_state": {"event_id": "event-2"},
                }
            ],
            0,
        )

        self.assertEqual(
            ["obs-2", "event-2"],
            payload["reflection_contract"]["allowed_evidence_refs"],
        )
        self.assertEqual(
            ["matched", "unexpected", "uncertain"],
            payload["reflection_contract"]["allowed_statuses"],
        )

    def test_response_schema_limits_reflection_status_to_transition_phase(self) -> None:
        base_contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "allowed_indices": [],
            "required_arguments": {},
        }
        without_previous = {
            **base_contract,
            "has_previous_transition": False,
        }
        with_previous = {
            **base_contract,
            "has_previous_transition": True,
        }

        first_schema = build_decision_response_schema(without_previous)
        later_schema = build_decision_response_schema(with_previous)

        self.assertEqual(
            ["not_applicable"],
            first_schema["properties"]["reflection"]["properties"]["status"]["enum"],
        )
        self.assertEqual(
            ["matched", "unexpected", "uncertain"],
            later_schema["properties"]["reflection"]["properties"]["status"]["enum"],
        )

    def test_repair_prompt_explains_reflection_contract_rules(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        invalid_decision = {
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
        }
        with patch.object(planner, "_request", return_value=invalid_decision) as request:
            planner.plan(
                {
                    "paused": True,
                    "player": {"present": True, "alive": True},
                    "menu": {},
                    "available_actions": ["direct_steer"],
                },
                2,
                [{"observation_id": "obs-2", "event_state": {}}],
            )
            planner.repair_plan(
                {
                    "paused": True,
                    "player": {"present": True, "alive": True},
                    "menu": {},
                    "available_actions": ["direct_steer"],
                },
                2,
                [{"observation_id": "obs-2", "event_state": {}}],
                invalid_decision,
                "reflection must cite evidence_refs from the observed transition",
            )

        system_prompt = request.call_args.args[0]
        self.assertIn("has_previous_transition", system_prompt)
        self.assertIn(
            "Do not invent evidence IDs",
            request.call_args.kwargs["messages"][-1]["content"],
        )

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
                "qa_observation": "No anomaly observed yet.",
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 0.7, "y": 0.7, "duration": 5},
                "expected_effect": "The player position should change along the chosen vector.",
                "reflection": {
                    "status": "not_applicable",
                    "summary": "No previous transition exists.",
                    "evidence_refs": [],
                    "candidate_id": "",
                    "reproduction_attempted": False,
                },
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
            {
                "qa_observation": "The upgrade dialog is blocking gameplay.",
                "tool": "game",
                "action": "select_upgrade",
                "arguments": {"index": 2},
                "expected_effect": "The selected ability should become owned or increase level.",
                "reflection": {
                    "status": "not_applicable",
                    "summary": "No previous transition exists.",
                    "evidence_refs": [],
                    "candidate_id": "",
                    "reproduction_attempted": False,
                },
            },
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

    def test_direct_steer_contract_rejects_missing_expected_effect(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "required_arguments": {},
            "has_previous_transition": True,
        }
        decision = {
            "qa_observation": "The prior movement reached the expected area.",
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 2.0},
            "reflection": {
                "status": "matched",
                "summary": "Position advanced east.",
                "evidence_refs": ["obs-2"],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        error = validate_decision_against_contract(decision, contract)

        self.assertIn("expected_effect", error or "")

    def test_source_read_is_exempt_from_expected_effect_but_requires_reflection(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["source_read"],
            "required_arguments": {},
            "has_previous_transition": True,
        }
        decision = {
            "qa_observation": "A runtime error warrants source inspection.",
            "tool": "source_read",
            "action": "",
            "arguments": {"path": "Assets/Scripts/Character/Character.cs"},
            "reflection": {
                "status": "unexpected",
                "summary": "The prior action emitted an exception.",
                "evidence_refs": ["obs-error"],
                "candidate_id": "runtime-exception",
                "reproduction_attempted": False,
            },
        }

        self.assertIsNone(validate_decision_against_contract(decision, contract))

    def test_extended_decision_schema_is_supported_by_strict_and_json_object_paths(self) -> None:
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "allowed_indices": [],
            "has_previous_transition": True,
        }
        schema = build_decision_response_schema(contract)
        self.assertIsNotNone(schema)
        self.assertIn("expected_effect", schema["required"])
        self.assertIn("reflection", schema["required"])
        strict = LLMPlanner(
            "qa", "gpt-4o-mini", TestCharter(), 5.0, api_key="test-key"
        )
        compatible = LLMPlanner(
            "qa",
            "provider-model",
            TestCharter(),
            5.0,
            api_url="https://provider.example/v1/chat/completions",
            api_key="test-key",
        )
        self.assertEqual("json_schema", strict._response_format(schema)["type"])
        self.assertEqual("json_object", compatible._response_format(schema)["type"])

    def test_planning_payload_contains_deterministic_observed_delta(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        previous = {
            "observed_delta": {
                "changes": [
                    {"path": "player.health", "before": 100.0, "after": 75.0}
                ]
            }
        }

        payload = planner._planning_payload(
            {"available_actions": ["observe"]}, 2, [previous], 0
        )

        self.assertEqual(
            previous["observed_delta"]["changes"],
            payload["prior_outcome"]["observed_delta"]["changes"],
        )
        self.assertTrue(payload["action_contract"]["has_previous_transition"])

    def test_observed_delta_reports_only_changed_tracked_values(self) -> None:
        delta = compute_observed_delta(
            {
                "observation_id": "obs-1",
                "player": {"health": 100.0, "level": 1},
                "progress": {"coins_gained": 0},
            },
            {
                "observation_id": "obs-2",
                "player": {"health": 75.0, "level": 1},
                "progress": {"coins_gained": 0},
            },
        )

        self.assertEqual("obs-1", delta["before_observation_id"])
        self.assertEqual("obs-2", delta["after_observation_id"])
        self.assertEqual(
            [{"path": "player.health", "before": 100.0, "after": 75.0}],
            delta["changes"],
        )

    def test_final_assessment_uses_only_confirmed_agent_hypotheses(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        with patch.object(planner, "_request", return_value={}) as request:
            planner.final_assessment(
                {
                    "metrics": {"steps": 3},
                    "confirmed_hypotheses": [{"candidate_id": "agent-found"}],
                    "anomalies": [{"kind": "health_ratio_out_of_range"}],
                    "fault_id": "health_ratio_out_of_range",
                    "ground_truth": {"bug_id": "health_ratio_out_of_range"},
                }
            )

        supplied = request.call_args.args[1]
        self.assertIn("agent-found", supplied)
        self.assertNotIn("health_ratio_out_of_range", supplied)
        self.assertNotIn("ground_truth", supplied)

    def test_instant_effect_action_is_followed_by_verification_observation(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def command(self, action: str, **parameters: object) -> dict[str, object]:
                self.calls.append((action, parameters))
                return {
                    "ok": True,
                    "observation_id": f"obs-{len(self.calls)}",
                    "command_id": f"command-{len(self.calls)}",
                }

        client = FakeClient()
        decision = {
            "decision_id": "decision-1",
            "action": "select_upgrade",
            "arguments": {"index": 0},
        }

        observation = execute_game_action(client, decision)

        self.assertEqual(["select_upgrade", "observe"], [call[0] for call in client.calls])
        self.assertEqual("obs-1", decision["action_observation_id"])
        self.assertEqual("obs-2", observation["observation_id"])

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
    def test_run_recorder_uses_new_reflection_prompt_version(self) -> None:
        recorder = RunRecorder(
            TEST_TEMP_ROOT / "prompt-version",
            "qa",
            "llm",
            9101,
            model="gpt-4o-mini",
        )

        self.assertEqual("qa-planning/v4", recorder.prompt_version)

    def test_llm_contract_failure_is_not_classified_as_infrastructure_error(self) -> None:
        recorder = RunRecorder(
            TEST_TEMP_ROOT / "llm-contract-verdict",
            "qa",
            "llm",
            9101,
            model="gpt-4o-mini",
        )

        verdict = run_module.build_session_verdict(
            argparse.Namespace(scenario_definition=None),
            recorder,
            "LLMContractError: invalid reflection",
            {},
        )

        self.assertEqual("contract_error", verdict["execution_status"])

    @staticmethod
    def annotation(reviewer_id: str, label: str) -> dict[str, object]:
        return {
            "reviewer_id": reviewer_id,
            "candidate_id": "candidate-1",
            "label": label,
            "matched_bug_id": "bug-1" if label == "valid_bug" else None,
            "reproducible": True,
            "spec_violation": label == "valid_bug",
            "system_caused": False,
            "difficulty": "medium",
            "evidence_refs": ["obs-1"],
            "notes": "",
        }

    def test_infrastructure_error_cannot_report_oracle_pass(self) -> None:
        with self.assertRaisesRegex(ValueError, "infrastructure_error"):
            build_run_verdict(
                execution_status="infrastructure_error",
                coverage_status="reached",
                oracle_verdict="pass",
                agent_detection="not_evaluated",
                evidence_refs=["obs-1"],
            )

    def test_single_reviewer_annotation_aggregation_is_provisional(self) -> None:
        aggregate = aggregate_annotations([self.annotation("reviewer-1", "valid_bug")])

        self.assertEqual("provisional", aggregate["status"])
        self.assertEqual("valid_bug", aggregate["candidates"][0]["label"])

    def test_three_reviewer_two_to_one_vote_uses_majority_label(self) -> None:
        aggregate = aggregate_annotations(
            [
                self.annotation("reviewer-1", "valid_bug"),
                self.annotation("reviewer-2", "valid_bug"),
                self.annotation("reviewer-3", "non_bug"),
            ]
        )

        candidate = aggregate["candidates"][0]
        self.assertEqual("reviewed", aggregate["status"])
        self.assertEqual("valid_bug", candidate["label"])
        self.assertEqual("majority", candidate["agreement"])

    def test_fault_identity_is_written_only_to_evaluator_manifest(self) -> None:
        output = TEST_TEMP_ROOT / "separated-channels"
        recorder = RunRecorder(
            output,
            "qa",
            "heuristic",
            9101,
            run_id="run-private",
            scenario_id="easy-health-ratio",
            preset="smoke",
            scenario_fingerprint="fingerprint-1",
            fault_id="health_ratio_out_of_range",
        )
        recorder.record(
            0,
            {"decision_id": "decision-1", "tool": "game", "action": "observe"},
            {
                "run_id": "run-private",
                "observation_id": "obs-1",
                "player": {},
                "progress": {},
                "menu": {},
                "world": {},
                "event_state": {},
            },
            0.1,
        )
        verdict = build_run_verdict(
            execution_status="completed",
            coverage_status="reached",
            oracle_verdict="fail",
            agent_detection="miss",
            evidence_refs=["obs-1"],
        )

        recorder.write_channel_artifacts(verdict, {"candidates": []})

        steps = (output / "steps.jsonl").read_text(encoding="utf-8")
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("health_ratio_out_of_range", steps)
        self.assertNotIn("ground_truth", steps)
        self.assertEqual("health_ratio_out_of_range", manifest["fault_id"])
        self.assertTrue((output / "verdict.json").is_file())
        self.assertTrue((output / "critic.json").is_file())
        self.assertEqual("", (output / "annotations.jsonl").read_text(encoding="utf-8"))

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
                "expected_effect": "Refresh observable state.",
                "reflection": {"status": "matched"},
                "observed_delta": {"changes": []},
                "hypothesis_state": {"status": "hypothesis"},
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
        self.assertEqual("Refresh observable state.", entry["expected_effect"])
        self.assertEqual("matched", entry["reflection"]["status"])
        self.assertEqual([], entry["observed_delta"]["changes"])
        self.assertEqual("hypothesis", entry["hypothesis_state"]["status"])

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
        recorder.add_api_usage(
            "planning_request",
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "request_count": 2},
            step=1,
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

    def test_fault_injection_sources_are_denied_for_search_and_read(self) -> None:
        root = TEST_TEMP_ROOT / "denied-source"
        fault_source = root / "Assets" / "Scripts" / "QA" / "QaFaultInjection.cs"
        fault_source.parent.mkdir(parents=True, exist_ok=True)
        fault_source.write_text("class QaFaultInjection {}", encoding="utf-8")
        scenario_config = root / "config" / "qa-scenarios.json"
        scenario_config.parent.mkdir(parents=True, exist_ok=True)
        scenario_config.write_text("{}", encoding="utf-8")
        tools = SourceTools(root)

        self.assertFalse(tools.search("QaFaultInjection")["ok"])
        self.assertFalse(tools.read("Assets/Scripts/QA/QaFaultInjection.cs")["ok"])
        self.assertFalse(tools.read("config/qa-scenarios.json")["ok"])


class HypothesisTrackerTests(unittest.TestCase):
    @staticmethod
    def candidate(**overrides: object) -> dict[str, object]:
        candidate: dict[str, object] = {
            "candidate_id": "candidate-1",
            "statement": "Upgrade acknowledgement did not change ability state.",
            "reflection_status": "unexpected",
            "evidence_refs": ["obs-1"],
            "reproduction_attempted": False,
        }
        candidate.update(overrides)
        return candidate

    def test_oracle_fields_cannot_confirm_an_agent_hypothesis(self) -> None:
        tracker = HypothesisTracker()

        state = tracker.observe(
            self.candidate(oracle_verdict="fail", ground_truth="bug-1")
        )

        self.assertEqual("hypothesis", state.status)
        self.assertEqual([], tracker.confirmed())

    def test_agent_reproduction_path_can_confirm_a_hypothesis(self) -> None:
        tracker = HypothesisTracker()
        tracker.observe(self.candidate())
        reproducing = tracker.observe(
            self.candidate(reproduction_attempted=True, evidence_refs=["obs-2"])
        )
        confirmed = tracker.observe(
            self.candidate(reproduction_attempted=True, evidence_refs=["obs-3"])
        )

        self.assertEqual("reproducing", reproducing.status)
        self.assertEqual("confirmed", confirmed.status)
        self.assertEqual("candidate-1", tracker.confirmed()[0]["candidate_id"])


class SessionMemoryTests(unittest.TestCase):
    @staticmethod
    def transition(step: int, **overrides: object) -> dict[str, object]:
        transition: dict[str, object] = {
            "step": step,
            "decision": {
                "decision_id": f"decision-{step}",
                "action": "direct_steer",
                "arguments": {},
            },
            "progress": {"level_time": float(step), "level": 1},
            "player": {"position": {"x": float(step), "y": 0.0}},
            "event_state": {},
        }
        transition.update(overrides)
        return transition

    def test_unresolved_hypothesis_survives_from_step_two_to_step_forty(self) -> None:
        memory = SessionMemory(recent_limit=6, token_budget=500)
        memory.add(
            self.transition(
                2,
                decision={
                    "decision_id": "decision-2",
                    "action": "observe",
                    "hypothesis_state": {
                        "candidate_id": "candidate-early",
                        "statement": "Health changed without damage evidence.",
                        "status": "hypothesis",
                        "evidence_refs": ["obs-2"],
                    },
                },
            )
        )
        for step in range(3, 41):
            memory.add(self.transition(step))

        summary = memory.summary()

        self.assertIn(
            "candidate-early",
            [item["candidate_id"] for item in summary["unresolved_hypotheses"]],
        )
        self.assertEqual(6, len(memory.recent_transitions()))

    def test_action_event_causality_survives_compression(self) -> None:
        memory = SessionMemory(recent_limit=2, token_budget=500)
        memory.add(
            self.transition(
                1,
                decision={
                    "decision_id": "decision-1",
                    "action": "direct_steer",
                    "arguments": {},
                },
                event_state={
                    "event_id": "event-1",
                    "caused_by_command_id": "command-1",
                    "type": "chest_collected",
                },
                command_id="command-1",
            )
        )
        memory.add(self.transition(2))
        memory.add(self.transition(3))

        links = memory.summary()["action_event_links"]

        self.assertEqual("chest_collected", links[0]["event_type"])
        self.assertEqual("direct_steer", links[0]["action"])
        self.assertEqual("event-1", links[0]["event_id"])

    def test_compressed_memory_stays_below_fixed_token_budget(self) -> None:
        memory = SessionMemory(recent_limit=6, token_budget=220)
        for step in range(100):
            memory.add(
                self.transition(
                    step,
                    event_state={
                        "type": "danger_spike",
                        "detail": "x" * 1000,
                        "event_id": f"event-{step}",
                    },
                )
            )

        self.assertLessEqual(memory.estimated_summary_tokens(), 220)

    def test_evaluator_channel_fields_are_removed_from_memory(self) -> None:
        memory = SessionMemory(recent_limit=6, token_budget=500)
        memory.add(
            self.transition(
                1,
                fault_id="health_ratio_out_of_range",
                ground_truth={"bug_id": "hidden-bug"},
                oracle_verdict="fail",
                evaluator={"coverage_status": "reached"},
            )
        )

        payload = json.dumps(
            {
                "recent": memory.recent_transitions(),
                "summary": memory.summary(),
            }
        )
        self.assertNotIn("health_ratio_out_of_range", payload)
        self.assertNotIn("hidden-bug", payload)
        self.assertNotIn("oracle_verdict", payload)

    def test_planning_payload_contains_recent_transitions_and_session_memory(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        transitions = [self.transition(step) for step in range(10)]

        payload = planner._planning_payload(
            {"available_actions": ["observe"]}, 10, transitions, 0
        )

        self.assertNotIn("recent_transitions", payload)
        self.assertNotIn("session_memory", payload)
        self.assertEqual(9, payload["prior_outcome"]["step"])


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
                        "protocol_version": "1.5",
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
    def test_fault_scenarios_declare_their_evaluator_private_fault_ids(self) -> None:
        expected = {
            "easy-health-ratio": "health_ratio_out_of_range",
            "easy-relative-position": "relative_position_mismatch",
            "medium-upgrade-effect": "upgrade_ack_without_effect",
            "medium-chest-transition": "chest_collected_without_state_transition",
            "hard-experience-drift": "experience_level_drift",
            "hard-restart-currency": "currency_leak_across_restart",
        }

        for scenario_id, fault_id in expected.items():
            self.assertEqual(fault_id, load_scenario(scenario_id).ground_truth.fault_id)
        self.assertIsNone(
            load_scenario("control-valid-observation").ground_truth.fault_id
        )

    def test_scenario_exit_code_uses_execution_axis_instead_of_legacy_smoke_checks(self) -> None:
        scenario_args = type("Args", (), {"scenario_definition": object()})()
        ad_hoc_args = type("Args", (), {"scenario_definition": None})()
        legacy_report = {"result": "fail"}
        completed = {"execution_status": "completed"}

        self.assertEqual(
            0,
            run_module.session_exit_code(scenario_args, legacy_report, completed),
        )
        self.assertEqual(
            1,
            run_module.session_exit_code(ad_hoc_args, legacy_report, completed),
        )

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
                "difficulty": "easy",
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
                "difficulty": "easy",
                "expected_behavior": "The run remains valid.",
                "reproduction_steps": ["Observe gameplay."],
                "review_status": "pending",
            },
        }

        with self.assertRaisesRegex(Exception, "unregistered oracle"):
            Scenario.model_validate(payload)

    def test_ground_truth_difficulty_must_match_scenario(self) -> None:
        payload = {
            "id": "invalid-ground-truth-difficulty",
            "difficulty": "easy",
            "preset": "smoke",
            "charter": {"objective": "Exercise ground-truth validation."},
            "seed_set": [9101, 9102, 9103],
            "limits": {"max_simulation_seconds": 60.0, "max_steps": 20},
            "coverage_target": "observe_player_state",
            "oracle": "health_ratio_consistency",
            "ground_truth": {
                "bug_id": "health_ratio_out_of_range",
                "fault_id": "health_ratio_out_of_range",
                "difficulty": "hard",
                "expected_behavior": "Reported health values remain internally consistent.",
                "reproduction_steps": ["Observe player health."],
                "review_status": "pending",
            },
        }

        with self.assertRaisesRegex(Exception, "difficulty must match"):
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

    def write_gate_run(
        self,
        scenario: Scenario,
        *,
        suffix: str = "primary",
        oracle_verdict: str | None = None,
        event_type: str = "horizon_complete",
        decision_arguments: dict[str, object] | None = None,
        player_health: float = 50.0,
    ) -> benchmark_module.RunResult:
        output = TEST_TEMP_ROOT / "deterministic-gate" / suffix / scenario.id
        output.mkdir(parents=True, exist_ok=True)
        observation_id = f"{scenario.id}-observation"
        event_id = f"{scenario.id}-event"
        step = {
            "decision": {
                "action": "steer",
                "arguments": decision_arguments or {"x": 1.0, "y": 0.0},
            },
            "observation": {
                "observation_id": observation_id,
                "scene": "Level 1",
                "phase": "active_gameplay",
                "player": {
                    "present": True,
                    "health": player_health,
                    "max_health": 100.0,
                    "health_ratio": player_health / 100.0,
                    "level": 3,
                    "exp": 4.0,
                    "next_level_exp": 10.0,
                    "exp_ratio": 0.4,
                },
                "progress": {"kills": 7, "level_time": 3.0},
                "inventory": {"abilities": []},
                "event_state": {"event_id": event_id, "type": event_type},
            },
        }
        (output / "steps.jsonl").write_text(
            json.dumps(step) + "\n",
            encoding="utf-8",
        )
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "scenario_id": scenario.id,
                    "seed": 9101,
                    "scenario_fingerprint": scenario_fingerprint(scenario),
                    "preset": scenario.preset,
                    "mode": "qa",
                    "policy": "heuristic",
                    "fault_id": scenario.ground_truth.fault_id,
                }
            ),
            encoding="utf-8",
        )
        expected_oracle = "fail" if scenario.ground_truth.fault_id else "pass"
        (output / "verdict.json").write_text(
            json.dumps(
                {
                    "execution_status": "completed",
                    "coverage_status": "reached",
                    "oracle_verdict": oracle_verdict or expected_oracle,
                    "evidence_refs": [observation_id, event_id],
                }
            ),
            encoding="utf-8",
        )
        return benchmark_module.RunResult(scenario.id, 9101, output, 0)

    def test_deterministic_gate_requires_six_faults_and_three_clean_controls(
        self,
    ) -> None:
        scenario_ids = (
            "easy-health-ratio",
            "easy-relative-position",
            "medium-upgrade-effect",
            "medium-chest-transition",
            "hard-experience-drift",
            "hard-restart-currency",
            "control-valid-observation",
            "control-normal-transitions",
            "control-long-progression",
        )
        scenarios = [load_scenario(scenario_id) for scenario_id in scenario_ids]
        results = [self.write_gate_run(scenario) for scenario in scenarios]

        summary = benchmark_module.verify_deterministic_gate(scenarios, results)

        self.assertEqual(6, summary["faults_detected"])
        self.assertEqual(3, summary["controls_passed"])
        self.assertEqual(0, summary["control_false_positives"])

    def test_deterministic_gate_rejects_unresolvable_evidence(self) -> None:
        scenarios = [
            load_scenario(scenario_id)
            for scenario_id in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS
        ]
        results = [self.write_gate_run(scenario) for scenario in scenarios]
        result = results[0]
        verdict_path = result.output_dir / "verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        verdict["evidence_refs"] = ["missing-observation"]
        verdict_path.write_text(json.dumps(verdict), encoding="utf-8")

        with self.assertRaisesRegex(ScenarioContractError, "unresolvable evidence"):
            benchmark_module.verify_deterministic_gate(scenarios, results)

    def test_deterministic_gate_rejects_a_partial_scenario_set(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        result = self.write_gate_run(scenario)

        with self.assertRaisesRegex(ScenarioContractError, "exact approved scenario set"):
            benchmark_module.verify_deterministic_gate([scenario], [result])

    def test_deterministic_gate_rejects_a_nonheuristic_artifact(self) -> None:
        scenarios = [
            load_scenario(scenario_id)
            for scenario_id in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS
        ]
        results = [self.write_gate_run(scenario) for scenario in scenarios]
        manifest_path = results[0].output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["policy"] = "llm"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ScenarioContractError, "qa heuristic artifact"):
            benchmark_module.verify_deterministic_gate(scenarios, results)

    def test_deterministic_gate_cli_requires_qa_heuristic_and_one_seed(self) -> None:
        scenarios = [
            load_scenario(scenario_id)
            for scenario_id in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS
        ]

        benchmark_module.validate_deterministic_gate_request(
            scenarios,
            [9101],
            mode="qa",
            policy="heuristic",
        )
        invalid_requests = (
            (scenarios, None, "qa", "heuristic"),
            (scenarios, [9101, 9102], "qa", "heuristic"),
            (scenarios, [9101], "player", "heuristic"),
            (scenarios, [9101], "qa", "llm"),
        )
        for requested_scenarios, seeds, mode, policy in invalid_requests:
            with self.subTest(seeds=seeds, mode=mode, policy=policy):
                with self.assertRaises(ScenarioContractError):
                    benchmark_module.validate_deterministic_gate_request(
                        requested_scenarios,
                        seeds,
                        mode=mode,
                        policy=policy,
                    )

        args = benchmark_module.parse_args(
            [
                "--game-exe",
                "player.app",
                "--output",
                "artifacts",
                "--deterministic-gate",
            ]
        )
        self.assertTrue(args.deterministic_gate)

    def test_deterministic_gate_cli_propagates_verifier_failure(self) -> None:
        args = argparse.Namespace(
            project_root=Path.cwd(),
            scenario=[],
            seed=[9101],
            mode="qa",
            policy="heuristic",
            game_exe=Path("player.app"),
            output=Path("artifacts"),
            model="",
            api_url=None,
            headless=True,
            quiet=True,
            deterministic_gate=True,
        )
        scenarios = [
            load_scenario(scenario_id)
            for scenario_id in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS
        ]
        with (
            patch.object(benchmark_module, "parse_args", return_value=args),
            patch.object(benchmark_module, "load_scenarios", return_value=scenarios),
            patch.object(benchmark_module, "run_benchmark", return_value=[]) as run,
            patch.object(
                benchmark_module,
                "verify_deterministic_gate",
                side_effect=ScenarioContractError("gate failed"),
            ) as verify,
            patch("builtins.print"),
            self.assertRaises(SystemExit) as exit_error,
        ):
            benchmark_module.main()

        self.assertEqual(2, exit_error.exception.code)
        run.assert_called_once()
        verify.assert_called_once_with(scenarios, [])

    def test_replay_verifier_ignores_run_ids_but_rejects_transition_drift(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        first = self.write_gate_run(scenario, suffix="replay-a")
        matching = self.write_gate_run(scenario, suffix="replay-b")

        benchmark_module.verify_replay(first, matching)

        divergent = self.write_gate_run(
            scenario,
            suffix="replay-c",
            event_type="danger_spike",
        )
        with self.assertRaisesRegex(ScenarioContractError, "transition sequence"):
            benchmark_module.verify_replay(first, divergent)

    def test_replay_verifier_rejects_identity_and_state_drift(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        first = self.write_gate_run(scenario, suffix="replay-state-a")
        argument_drift = self.write_gate_run(
            scenario,
            suffix="replay-state-b",
            decision_arguments={"x": -1.0, "y": 0.0},
        )
        with self.assertRaisesRegex(ScenarioContractError, "transition sequence"):
            benchmark_module.verify_replay(first, argument_drift)

        health_drift = self.write_gate_run(
            scenario,
            suffix="replay-state-c",
            player_health=40.0,
        )
        with self.assertRaisesRegex(ScenarioContractError, "transition sequence"):
            benchmark_module.verify_replay(first, health_drift)

        failed = self.write_gate_run(scenario, suffix="replay-state-d")
        failed = benchmark_module.RunResult(
            failed.scenario_id,
            failed.seed,
            failed.output_dir,
            1,
        )
        with self.assertRaisesRegex(ScenarioContractError, "non-zero"):
            benchmark_module.verify_replay(first, failed)

    def test_replay_verifier_requires_complete_manifest_configuration(self) -> None:
        scenario = load_scenario("easy-health-ratio")
        first = self.write_gate_run(scenario, suffix="replay-config-a")
        second = self.write_gate_run(scenario, suffix="replay-config-b")
        for result in (first, second):
            manifest_path = result.output_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["policy"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ScenarioContractError, "required configuration"):
            benchmark_module.verify_replay(first, second)


class EvaluationTests(unittest.TestCase):
    @staticmethod
    def transition(
        observation_id: str,
        *,
        action: str = "observe",
        player: dict[str, object] | None = None,
        event_state: dict[str, object] | None = None,
        **observation_overrides: object,
    ) -> dict[str, object]:
        observation = {
            "observation_id": observation_id,
            "player": player or {},
            "world": {},
            "progress": {},
            "inventory": {},
            "event_state": event_state or {},
        }
        observation.update(observation_overrides)
        return {
            "decision": {"action": action},
            "observation": observation,
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

    def test_six_injected_fault_signals_are_detected_by_their_oracles(self) -> None:
        cases = {
            "easy-health-ratio": [
                self.transition(
                    "obs-health",
                    player={
                        "present": True,
                        "health": 50.0,
                        "max_health": 100.0,
                        "health_ratio": 1.25,
                    },
                )
            ],
            "easy-relative-position": [
                self.transition(
                    "obs-position",
                    player={"present": True, "position": {"x": 10.0, "y": 5.0}},
                    world={
                        "qa_entities": [
                            {
                                "x": 13.0,
                                "y": 8.0,
                                "relative_x": 10.0,
                                "relative_y": 3.0,
                            }
                        ]
                    },
                )
            ],
            "medium-upgrade-effect": [
                self.transition(
                    "obs-upgrade-before",
                    inventory={"abilities": [{"type": "Axe", "level": 0, "owned": False}]},
                ),
                self.transition(
                    "obs-upgrade-after",
                    action="select_upgrade",
                    inventory={"abilities": [{"type": "Axe", "level": 0, "owned": False}]},
                ),
            ],
            "medium-chest-transition": [
                self.transition(
                    "obs-chest-before",
                    world={"chest_count": 1},
                    progress={"coins_gained": 0, "damage_dealt": 0.0, "damage_taken": 0.0},
                ),
                self.transition(
                    "obs-chest-after",
                    world={"chest_count": 1},
                    progress={"coins_gained": 0, "damage_dealt": 0.0, "damage_taken": 0.0},
                    event_state={
                        "type": "chest_collected",
                        "event_id": "event-chest",
                    },
                ),
            ],
            "hard-experience-drift": [
                self.transition(
                    "obs-exp",
                    player={
                        "present": True,
                        "level": 3,
                        "exp": 13.0,
                        "next_level_exp": 20.0,
                        "exp_ratio": 0.5,
                    },
                )
            ],
            "hard-restart-currency": [
                self.transition(
                    "obs-restart-before",
                    progress={"level_time": 30.0, "coins_gained": 7},
                ),
                self.transition(
                    "obs-restart-after",
                    action="restart",
                    progress={"level_time": 0.0, "coins_gained": 7},
                ),
            ],
        }

        for scenario_id, transitions in cases.items():
            with self.subTest(scenario=scenario_id):
                result = evaluate_oracle(load_scenario(scenario_id), transitions)
                self.assertEqual("fail", result.verdict)
                self.assertTrue(result.evidence_refs)

    def test_experience_oracle_allows_the_upgrade_dialog_boundary(self) -> None:
        transitions = [
            self.transition(
                "obs-upgrade-open",
                player={
                    "present": True,
                    "health": 100.0,
                    "max_health": 100.0,
                    "health_ratio": 1.0,
                    "level": 3,
                    "exp": 5.0,
                    "next_level_exp": 5.0,
                    "exp_ratio": 1.0,
                },
                phase="upgrade_selection",
            ),
            self.transition(
                "obs-upgrade-applied",
                action="select_upgrade",
                player={
                    "present": True,
                    "health": 100.0,
                    "max_health": 100.0,
                    "health_ratio": 1.0,
                    "level": 3,
                    "exp": 5.0,
                    "next_level_exp": 20.0,
                    "exp_ratio": 0.25,
                },
                phase="active_gameplay",
            ),
        ]

        result = evaluate_oracle(
            load_scenario("control-long-progression"),
            transitions,
        )

        self.assertEqual("pass", result.verdict)

    def test_health_oracle_allows_consistent_game_over_damage_overshoot(self) -> None:
        transitions = [
            self.transition(
                "obs-game-over",
                player={
                    "present": True,
                    "alive": False,
                    "health": -1.0,
                    "max_health": 100.0,
                    "health_ratio": -0.01,
                },
                phase="game_over",
            )
        ]

        result = evaluate_oracle(
            load_scenario("control-valid-observation"),
            transitions,
        )

        self.assertEqual("pass", result.verdict)

    def test_health_oracle_rejects_consistent_out_of_range_active_health(self) -> None:
        transitions = [
            self.transition(
                "obs-active-invalid-health",
                player={
                    "present": True,
                    "alive": True,
                    "health": -1.0,
                    "max_health": 100.0,
                    "health_ratio": -0.01,
                },
                phase="active_gameplay",
            )
        ]

        result = evaluate_oracle(load_scenario("control-valid-observation"), transitions)

        self.assertEqual("fail", result.verdict)

    def test_experience_oracle_rejects_invalid_upgrade_dialog_values(self) -> None:
        transitions = [
            self.transition(
                "obs-upgrade-invalid",
                player={
                    "present": True,
                    "level": 3,
                    "exp": 13.0,
                    "next_level_exp": 20.0,
                    "exp_ratio": 0.5,
                },
                phase="upgrade_selection",
            )
        ]

        result = evaluate_oracle(load_scenario("hard-experience-drift"), transitions)

        self.assertEqual("fail", result.verdict)


# Guard tests for the hybrid bridge assist (--policy hybrid).
#
# These pin the behavior that must NOT change when the per-frame survival assist
# lands: the pure-llm control policy declaration, the cached system prompt prefix,
# the scenario fingerprints, and the deterministic gate's heuristic-only rule.
LEGACY_CONTROL_POLICY = {
    "planner_authority": "llm_when_policy_is_llm",
    "automatic_enemy_avoidance": False,
    "automatic_chest_targeting": False,
    "automatic_direction_correction": False,
    "bridge_role": "hold_the_llm_vector_and_detect_events_only",
    "priority_order": [
        "survive",
        "collect_reachable_chests",
        "net_heading_progress",
        "coverage",
    ],
}

NO_ASSIST_PROMPT_CLAIMS = (
    "The executor never silently corrects your vector.",
    "Unity does NOT automatically avoid enemies, choose a chest, attract toward a "
    "chest, enforce the requested heading, or alter your direction. It only holds "
    "your chosen vector every frame and detects events.",
)

APPROVED_SCENARIO_FINGERPRINTS = {
    "easy-health-ratio": "f6ec3bd7803262bdcb7cd6e47f2221f9200a7e29ebfcd866bc2d3667b674a419",
    "easy-relative-position": "3fd96bdd7fafe56eedd3cc694bbc2063c7f056620c479f79661dfbd9a909dd6e",
    "medium-upgrade-effect": "5dfee63a99e24e03768d02ad7248d6e4320d2ba493581effa263e91f9e976e13",
    "medium-chest-transition": "85d785eed36d4ab222ce3cf82d7dfbfe39d3500f4a9a9e3ffdf0884928b062d2",
    "hard-experience-drift": "3db768e89df56af3612b2ccc6a314756280d6d359e266d3acce38a32a3e1d982",
    "hard-restart-currency": "287589be5eaf4074b45d14d9ce03ed7f3461ac0898ed8b5c710947cef2906caf",
    "control-valid-observation": "368b548b4ab9e04b7ee4154ae87b894f4d3bc9cc6973eebfabc25a964bcd7643",
    "control-normal-transitions": "efd037b6eacca55e8affeee6b5ea9fa451e5c6a547bb63b8a98a28aab879523f",
    "control-long-progression": "458d405383e7339d53596562eda9611d0baaa3e566966b88c3dff720f147975f",
}


class BridgeAssistGuardTests(unittest.TestCase):
    def test_default_charter_control_policy_is_unchanged(self) -> None:
        """The pure-llm charter must keep advertising zero bridge intervention."""
        self.assertEqual(
            LEGACY_CONTROL_POLICY, TestCharter().as_dict()["control_policy"]
        )

    def test_llm_prompt_declares_no_bridge_correction(self) -> None:
        """The cached prompt prefix states the bridge never alters the vector.

        Both claims become false under --policy hybrid, so both must be made
        conditional rather than edited in place.
        """
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        prompt = planner._planning_system_prompt()

        for claim in NO_ASSIST_PROMPT_CLAIMS:
            self.assertIn(claim, prompt)

    def test_scenario_fingerprints_are_stable(self) -> None:
        """Charter fields added for hybrid must not reach CharterDefinition.

        scenario_fingerprint hashes the scenario model dump; changing it would
        invalidate the deterministic gate against every stored artifact.
        """
        for scenario_id, expected in APPROVED_SCENARIO_FINGERPRINTS.items():
            with self.subTest(scenario=scenario_id):
                self.assertEqual(
                    expected, scenario_fingerprint(load_scenario(scenario_id))
                )

    def test_deterministic_gate_cli_rejects_a_hybrid_request(self) -> None:
        scenarios = [
            load_scenario(scenario_id)
            for scenario_id in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS
        ]

        with self.assertRaisesRegex(
            ScenarioContractError, "qa mode and heuristic policy"
        ):
            benchmark_module.validate_deterministic_gate_request(
                scenarios,
                [9101],
                mode="qa",
                policy="hybrid",
            )


if __name__ == "__main__":
    unittest.main()
