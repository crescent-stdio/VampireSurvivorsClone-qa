from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
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
from .evaluation import evaluate_oracle, fault_evidence_refs, scenario_verdict_axes
from . import reevaluate as reevaluate_module
from . import detection as detection_module
from . import inspector as inspector_module
from .scenarios import (
    Scenario,
    ScenarioContractError,
    load_scenario,
    load_v4_ground_truth,
    scenario_fingerprint,
)
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

    def test_decision_schema_covers_a_phase_with_several_allowed_calls(self) -> None:
        """game_over offers restart and return_to_menu, and used to get no schema at all.

        Without one the model may drop required keys: four of five failed
        corrections were "qa_observation must be a string", which the schema
        would have made impossible.
        """
        contract = {
            "phase": "game_over",
            "allowed_calls": ["game.restart", "game.return_to_menu"],
            "has_previous_transition": True,
            "reflection_contract": {"allowed_statuses": ["matched", "unexpected", "uncertain"]},
        }

        schema = build_decision_response_schema(contract)

        self.assertIsNotNone(schema)
        self.assertEqual(
            ["restart", "return_to_menu"], schema["properties"]["action"]["enum"]
        )
        self.assertIn("qa_observation", schema["required"])
        self.assertIn("reflection", schema["required"])
        # Structured Outputs rejects an open object: "additionalProperties is
        # required to be supplied and to be false". Both calls here take no
        # arguments, so one closed empty shape covers the phase.
        self.assertEqual(
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            schema["properties"]["arguments"],
        )

    def test_decision_schema_branches_arguments_when_shapes_differ(self) -> None:
        """direct_steer and use_item share active_gameplay with different arguments.

        Strict mode also requires every property to be required, so a union of
        both shapes is invalid; each closed shape becomes an anyOf branch.
        """
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer", "game.use_item"],
            "allowed_indices": [0, 2],
            "has_previous_transition": True,
            "reflection_contract": {"allowed_statuses": ["matched", "unexpected", "uncertain"]},
        }

        arguments = build_decision_response_schema(contract)["properties"]["arguments"]

        self.assertEqual(2, len(arguments["anyOf"]))
        for branch in arguments["anyOf"]:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(sorted(branch["properties"]), sorted(branch["required"]))
        self.assertEqual(
            [0, 2], arguments["anyOf"][1]["properties"]["index"]["enum"]
        )

    def test_decision_schema_keeps_single_call_arguments_strict(self) -> None:
        """Generalising to several calls must not loosen the single-call shape."""
        contract = {
            "phase": "active_gameplay",
            "allowed_calls": ["game.direct_steer"],
            "has_previous_transition": False,
            "reflection_contract": {"allowed_statuses": ["not_applicable"]},
        }

        schema = build_decision_response_schema(contract)

        self.assertEqual(["direct_steer"], schema["properties"]["action"]["enum"])
        self.assertEqual(
            ["x", "y", "duration", "intent", "target_id"],
            schema["properties"]["arguments"]["required"],
        )

    def test_repair_prompt_replaces_an_action_the_phase_forbids(self) -> None:
        """A game-over episode died twice because the correction told the model to keep its action.

        At game_over the action itself is what the contract rejects, so an
        instruction to preserve tool, action and arguments guarantees the second
        response repeats the violation and the episode fails closed. The
        correction must name the allowed calls and require replacing the action.
        """
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        invalid_decision = {
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
        }
        game_over = {
            "paused": True,
            "player": {"present": True, "alive": False},
            "menu": {},
            "available_actions": ["observe", "restart", "return_to_menu"],
        }

        with patch.object(planner, "_request", return_value=invalid_decision) as request:
            planner.plan(game_over, 3, [{"observation_id": "obs-3", "event_state": {}}])
            planner.repair_plan(
                game_over,
                3,
                [{"observation_id": "obs-3", "event_state": {}}],
                invalid_decision,
                "game.direct_steer is invalid in phase game_over; "
                "allowed: ['game.restart', 'game.return_to_menu']",
            )

        correction = json.loads(request.call_args.kwargs["messages"][-1]["content"])

        self.assertEqual(
            ["game.restart", "game.return_to_menu"], correction["allowed_calls"]
        )
        # allowed_calls carry the game. prefix but decision.action does not, and a
        # correction that echoed the call name produced "game.game.restart is invalid".
        self.assertEqual(
            ["restart", "return_to_menu"], correction["allowed_actions"]
        )
        self.assertIn("replace", correction["instruction"].lower())
        self.assertIn("allowed_actions", correction["instruction"])
        self.assertNotIn(
            "Preserve valid gameplay tool, action, and arguments;",
            correction["instruction"],
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

    def test_every_openai_model_gets_strict_structured_output(self) -> None:
        """Schema enforcement must not change when the model does.

        Gating it on gpt-4o-mini meant swapping models moved capability and
        structural enforcement at once, so neither could be measured alone.
        """
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        for model in ("gpt-4o", "gpt-4.1", "o4-mini"):
            with self.subTest(model=model):
                planner = LLMPlanner("qa", model, TestCharter(), 5.0, api_key="test-key")

                self.assertEqual("json_schema", planner._response_format(schema)["type"])

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

        self.assertEqual("qa-planning/v7", recorder.prompt_version)

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

    def test_atomic_command_write_retries_transient_windows_sharing_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "command.json"
            real_replace = os.replace
            attempts = 0

            def transient_replace(source: Path, target: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("simulated Windows sharing violation")
                real_replace(source, target)

            with patch.object(bridge_client_module.os, "replace", side_effect=transient_replace):
                with patch.object(bridge_client_module.time, "sleep"):
                    BridgeClient._write_json_atomic(destination, {"id": "command-1"})

            self.assertEqual(2, attempts)
            self.assertEqual({"id": "command-1"}, json.loads(destination.read_text()))

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


def fake_opener(payload: str):
    """Stand in for urllib.request.urlopen, returning a fixed body."""

    class _Response:
        def read(self) -> bytes:
            return payload.encode("utf-8")

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def opener(*_args, **_kwargs) -> "_Response":
        return _Response()

    return opener


def health_step(step: int, run_id: str, health: float, ratio: float) -> dict[str, object]:
    """One recorded transition carrying a player observation."""
    return {
        "step": step,
        "observation": {
            "observation_id": f"{run_id}-obs-{step:08d}",
            "phase": "active_gameplay",
            "player": {
                "present": True,
                "alive": True,
                "health": health,
                "max_health": 100.0,
                "health_ratio": ratio,
            },
        },
    }


def trace(run_id: str, ratio_is_faulty: bool, count: int = 4) -> list[dict[str, object]]:
    return [
        health_step(i, run_id, 98.0, 1.25 if ratio_is_faulty else 0.98)
        for i in range(count)
    ]


def all_known_fault_ids() -> set[str]:
    ids = {
        scenario.ground_truth.fault_id
        for scenario in (load_scenario(i) for i in benchmark_module.DETERMINISTIC_GATE_SCENARIO_IDS)
        if scenario.ground_truth.fault_id
    }
    ids.update(
        str(entry.get("fault_id"))
        for entry in load_v4_ground_truth().values()
        if entry.get("fault_id")
    )
    return ids


class DetectionRubricTests(unittest.TestCase):
    def test_rubric_covers_every_known_fault(self) -> None:
        rubric = detection_module.load_rubric()

        self.assertEqual(all_known_fault_ids(), set(rubric.faults))

    def test_no_rubric_term_can_trip_the_gate_fault_id_check(self) -> None:
        """benchmark.verify_deterministic_gate fails if a fault_id appears in steps.jsonl.

        The rubric asks the agent to write these phrases, so a term that contains or
        reconstructs a fault id would make a *better* agent break the gate.
        """
        rubric = detection_module.load_rubric()
        fault_ids = all_known_fault_ids()
        normalized_ids = {detection_module.normalize(fault_id) for fault_id in fault_ids}

        for fault_id, entry in rubric.faults.items():
            for term in [*entry.topic_terms, *entry.symptom_terms]:
                with self.subTest(fault=fault_id, term=term):
                    for known in fault_ids:
                        self.assertNotIn(known, term)
                    self.assertNotIn(detection_module.normalize(term), normalized_ids)
            for topic in entry.topic_terms:
                for symptom in entry.symptom_terms:
                    joined = detection_module.normalize(f"{topic} {symptom}")
                    with self.subTest(fault=fault_id, joined=joined):
                        self.assertNotIn(joined, normalized_ids)

    def test_unobservable_faults_carry_a_reason(self) -> None:
        rubric = detection_module.load_rubric()
        unobservable = {
            fault_id
            for fault_id, entry in rubric.faults.items()
            if not entry.observable_in_agent_channel
        }

        # Both are raw-vs-player_view mismatches, and state_channels strips player_view.
        self.assertEqual({"health_bar_desync", "experience_display_drift"}, unobservable)
        for fault_id in unobservable:
            self.assertTrue(rubric.faults[fault_id].unobservable_reason)

    def test_a_wrong_schema_is_rejected(self) -> None:
        path = TEST_TEMP_ROOT / "bad-rubric.json"
        path.write_text(json.dumps({"schema": "nope", "faults": {}}), encoding="utf-8")

        with self.assertRaises(detection_module.DetectionContractError):
            detection_module.load_rubric(path)

    def test_normalization_folds_case_separators_and_whitespace(self) -> None:
        self.assertEqual(
            "health ratio is out of range",
            detection_module.normalize("  Health_Ratio  is   OUT-OF-RANGE "),
        )


def claim_step(step: int, run_id: str, text: str, refs: list[str]) -> dict[str, object]:
    """A recorded transition whose decision carries agent-authored text."""
    return {
        "step": step,
        "run_id": run_id,
        "decision": {
            "plan": "keep moving",
            "hypothesis": "",
            "qa_observation": text,
            "expected_effect": "",
            "arguments": {"x": 1.0, "y": 0.0, "interrupt_health_ratio": 0.3},
            "reflection": {"status": "unexpected", "summary": "", "evidence_refs": refs,
                           "candidate_id": "c-1", "reproduction_attempted": False},
        },
    }


FINDING = "health_ratio reads 1.25 which is inconsistent with health 98 of max_health 100"


class AgentDetectionVerdictTests(unittest.TestCase):
    """The scored axis has to reach verdict.json and its own artifact."""

    def recorder(self, name: str, rows: list[dict[str, object]], fault_id: str | None) -> RunRecorder:
        recorder = RunRecorder(
            TEST_TEMP_ROOT / name, "qa", "llm", 9102, model="gpt-4o-mini", fault_id=fault_id
        )
        recorder.steps = rows
        return recorder

    def scenario_args(self) -> argparse.Namespace:
        return argparse.Namespace(scenario_definition=load_scenario("easy-health-ratio"))

    def test_a_silent_agent_on_a_fault_run_is_scored_a_miss(self) -> None:
        rows = trace("run-fault", ratio_is_faulty=True)

        verdict = run_module.build_session_verdict(
            self.scenario_args(),
            self.recorder("detect-miss", rows, "health_ratio_out_of_range"),
            None,
            {},
        )

        self.assertEqual("fail", verdict["oracle_verdict"])
        self.assertEqual("miss", verdict["agent_detection"])

    def test_an_agent_that_names_the_defect_is_scored_a_match(self) -> None:
        rows = trace("run-fault2", ratio_is_faulty=True)
        rows[2]["decision"] = {
            "qa_observation": FINDING,
            "reflection": {"status": "unexpected", "summary": "", "candidate_id": "c-1",
                           "evidence_refs": ["run-fault2-obs-00000002"],
                           "reproduction_attempted": False},
        }

        verdict = run_module.build_session_verdict(
            self.scenario_args(),
            self.recorder("detect-match", rows, "health_ratio_out_of_range"),
            None,
            {},
        )

        self.assertEqual("match", verdict["agent_detection"])

    def test_the_detection_artifact_records_its_own_authority(self) -> None:
        recorder = self.recorder(
            "detect-artifact", trace("run-fault3", ratio_is_faulty=True), "health_ratio_out_of_range"
        )
        verdict = run_module.build_session_verdict(self.scenario_args(), recorder, None, {})
        recorder.write_channel_artifacts(verdict, {})

        payload = json.loads(
            (recorder.output_dir / "agent-detection.json").read_text(encoding="utf-8")
        )
        self.assertEqual("qa-agent-detection/v1", payload["schema_version"])
        self.assertEqual("miss", payload["status"])
        self.assertIn("sole authority", payload["authority_note"])
        self.assertEqual(
            "", (recorder.output_dir / "annotations.jsonl").read_text(encoding="utf-8")
        )

    def test_the_detection_axis_never_moves_the_final_verdict(self) -> None:
        """The game's verdict must not depend on how well the agent narrated it."""
        rows = trace("run-fault4", ratio_is_faulty=True)
        base = run_module.build_session_verdict(
            self.scenario_args(), self.recorder("detect-fv-a", rows, "health_ratio_out_of_range"), None, {}
        )
        found = list(rows)
        found[2] = dict(found[2])
        found[2]["decision"] = {
            "qa_observation": FINDING,
            "reflection": {"status": "unexpected", "summary": "", "candidate_id": "c-1",
                           "evidence_refs": ["run-fault4-obs-00000002"],
                           "reproduction_attempted": False},
        }
        detected = run_module.build_session_verdict(
            self.scenario_args(), self.recorder("detect-fv-b", found, "health_ratio_out_of_range"), None, {}
        )

        self.assertNotEqual(base["agent_detection"], detected["agent_detection"])
        self.assertEqual(base["final_verdict"], detected["final_verdict"])


LUNA_ANSWERS = [
    {"field": "player.health_ratio", "computed_value": 1.0, "reported_value": 1.25,
     "statement": "This exceeds the valid fraction range and is a QA finding.",
     "evidence_refs": ["run-obs-00000003"]},
    {"field": "player.health_ratio", "computed_value": 0.96, "reported_value": 1.25,
     "statement": "This is an internal state inconsistency requiring recheck.",
     "evidence_refs": ["run-obs-00000003"]},
    {"field": "player.health_ratio", "computed_value": 0.96, "reported_value": 1.25,
     "statement": "These disagree, indicating a health-ratio consistency anomaly.",
     "evidence_refs": ["run-obs-00000003"]},
]


class InspectorPayloadTests(unittest.TestCase):
    """The inspector must never be handed the answer."""

    EVALUATOR_KEYS = (
        "fault_id", "ground_truth", "oracle", "oracle_verdict",
        "coverage_status", "verdict", "manifest", "bug_id", "expected_behavior",
    )

    def raw_transition(self) -> dict[str, object]:
        """A transition shaped like the real recorder output, evaluator state included."""
        return {
            "observation": {
                "observation_id": "run-obs-00000003",
                "phase": "active_gameplay",
                "fault_id": "health_ratio_out_of_range",
                "ground_truth": {"bug_id": "BUG-X", "expected_behavior": "should be consistent"},
                "player_view": {"health_ratio": 0.96},
                "oracle": {"oracle_verdict": "fail"},
                "player": {"present": True, "alive": True, "health": 96.0,
                           "max_health": 100.0, "health_ratio": 1.25},
                "world": {"enemy_count": 3, "danger_score": 1.0},
                "progress": {"level_time": 12.0},
            }
        }

    def test_no_evaluator_state_survives_into_the_payload(self) -> None:
        blob = json.dumps(
            inspector_module.build_inspection_payload([self.raw_transition()]),
            ensure_ascii=False,
        )

        for key in self.EVALUATOR_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(f'"{key}"', blob)
        self.assertNotIn("BUG-X", blob)

    def test_the_fields_needed_to_find_the_bug_do_survive(self) -> None:
        payload = inspector_module.build_inspection_payload([self.raw_transition()])
        player = payload["observations"][0]["player"]

        self.assertEqual(96.0, player["health"])
        self.assertEqual(100.0, player["max_health"])
        self.assertEqual(1.25, player["health_ratio"])
        self.assertEqual("run-obs-00000003", payload["observations"][0]["observation_id"])

    def test_an_empty_trace_needs_no_api_call(self) -> None:
        self.assertEqual(
            {"schema_version": "qa-inspection/v2", "findings": []},
            inspector_module.inspect_trace(None, []),
        )

    def test_malformed_model_output_is_dropped_not_raised(self) -> None:
        cases = (None, {"findings": "nope"}, {"findings": [{"field": "x"}]},
                 {"findings": [{"field": "x", "computed_value": "a", "reported_value": 1}]})
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual([], inspector_module.normalize_findings(payload))


class InspectorScoringTests(unittest.TestCase):
    """Structured findings are scored by their numbers, not their adjectives."""

    def score(self, **overrides: object) -> object:
        kwargs: dict[str, object] = {
            "fault_id": "health_ratio_out_of_range",
            "policy": "heuristic",
            "has_agent_text_channel": True,
            "trace_completeness": "complete",
            "oracle_verdict": "fail",
            "transitions": [],
            "fault_refs": ["run-obs-00000003"],
            "hypotheses": [],
            "llm_assessment": None,
            "inspection": None,
        }
        kwargs.update(overrides)
        return detection_module.score_agent_detection(**kwargs)

    def test_every_real_luna_answer_scores_a_match(self) -> None:
        """The keyword rubric scored only one of these three; that is why this path exists."""
        for index, finding in enumerate(LUNA_ANSWERS):
            with self.subTest(answer=index):
                result = self.score(inspection={"findings": [finding]})

                self.assertEqual("match", result.status)
                self.assertEqual(["inspection"], result.matched_surfaces)
                self.assertEqual(["run-obs-00000003"], result.cited_evidence_refs)

    def test_the_keyword_rubric_scores_fewer_of_them_than_the_numbers_do(self) -> None:
        """The whole reason the numeric path exists.

        Judged on wording, these statements score 0 of 3 -- they say "exceeds",
        "inconsistency" and "disagree", and the rubric lists "exceeds" only alongside a
        topic term that lives in the field name rather than the sentence. Judged on
        their numbers, all three are unambiguous.
        """
        entry = detection_module.load_rubric().faults["health_ratio_out_of_range"]
        by_words = sum(
            bool(
                detection_module._terms_hit(answer["statement"], entry.topic_terms)
                and detection_module._terms_hit(answer["statement"], entry.symptom_terms)
            )
            for answer in LUNA_ANSWERS
        )
        by_numbers = sum(
            self.score(inspection={"findings": [answer]}).status == "match"
            for answer in LUNA_ANSWERS
        )

        self.assertEqual(3, by_numbers)
        self.assertLess(by_words, by_numbers)

    def test_agreeing_values_are_not_a_finding_however_it_is_worded(self) -> None:
        alarmed = {
            "field": "player.health_ratio", "computed_value": 0.96, "reported_value": 0.96,
            "statement": "ALARMING mismatch, inconsistent, exceeds, does not match!",
            "evidence_refs": ["run-obs-00000003"],
        }

        self.assertEqual("miss", self.score(inspection={"findings": [alarmed]}).status)

    def test_a_finding_citing_an_unrejected_transition_is_not_a_match(self) -> None:
        elsewhere = {**LUNA_ANSWERS[1], "evidence_refs": ["run-obs-00000099"]}

        self.assertEqual("miss", self.score(inspection={"findings": [elsewhere]}).status)

    def test_an_inspector_finding_on_a_control_run_is_a_false_positive(self) -> None:
        result = self.score(fault_id=None, inspection={"findings": [LUNA_ANSWERS[0]]})

        self.assertEqual("false_positive", result.status)

    def test_a_silent_inspector_on_a_control_run_is_not_a_false_positive(self) -> None:
        result = self.score(fault_id=None, inspection={"findings": []})

        self.assertEqual("not_evaluated", result.status)

    def test_malformed_findings_do_not_crash_the_scorer(self) -> None:
        for payload in ({"findings": "nope"}, {"findings": [{"field": "x"}]}, {}):
            with self.subTest(payload=payload):
                self.assertIn(self.score(inspection=payload).status, {"miss", "not_evaluated"})


class AgentDetectionScoringTests(unittest.TestCase):
    def score(self, **overrides: object) -> object:
        kwargs: dict[str, object] = {
            "fault_id": "health_ratio_out_of_range",
            "policy": "llm",
            "trace_completeness": "complete",
            "oracle_verdict": "fail",
            "transitions": [],
            "fault_refs": ["run-obs-00000003"],
            "hypotheses": [],
            "llm_assessment": None,
            "has_agent_text_channel": None,
            "inspection": None,
        }
        kwargs.update(overrides)
        return detection_module.score_agent_detection(**kwargs)

    def test_naming_the_defect_with_a_flagged_ref_is_a_match(self) -> None:
        result = self.score(
            transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])]
        )

        self.assertEqual("match", result.status)
        self.assertEqual(["run-obs-00000003"], result.cited_evidence_refs)
        self.assertIn("health ratio", result.matched_terms)

    def test_correct_words_citing_an_unrelated_transition_are_not_a_match(self) -> None:
        result = self.score(
            transitions=[claim_step(0, "run", FINDING, ["run-obs-00000099"])]
        )

        self.assertEqual("miss", result.status)

    def test_a_topic_word_without_a_symptom_is_narration_not_a_finding(self) -> None:
        result = self.score(
            transitions=[
                claim_step(0, "run", "health ratio is 1.25 and enemies are close", ["run-obs-00000003"])
            ]
        )

        self.assertEqual("miss", result.status)

    def test_arguments_are_never_scanned_for_rubric_terms(self) -> None:
        """The charter injects interrupt_health_ratio into every direct_steer.

        Scanning the whole decision matched "health ratio" on 16 of 18 steps of a
        real run where the agent never mentioned it once.
        """
        silent = claim_step(0, "run", "", ["run-obs-00000003"])
        silent["decision"]["qa_observation"] = "moving toward the pickup"
        silent["decision"]["reflection"]["summary"] = "it is inconsistent"

        result = self.score(transitions=[silent])

        self.assertEqual("miss", result.status)

    def test_terms_match_on_word_boundaries_not_substrings(self) -> None:
        """"exp" and "xp" both sit inside "expected".

        A substring scan scored the experience-drift fault on the sentence "the
        expected effect does not match the observation", which is about steering.
        """
        rubric = detection_module.load_rubric()
        entry = rubric.faults["experience_level_drift"]
        innocuous = "the expected effect does not match the observation"

        self.assertEqual([], detection_module._terms_hit(innocuous, entry.topic_terms))
        self.assertNotEqual(
            [], detection_module._terms_hit("exp does not match exp ratio", entry.topic_terms)
        )

    def test_inflected_symptom_forms_are_recognized(self) -> None:
        """Word-boundary matching makes each verb form a distinct token.

        gpt-4o wrote "should not exceed 1.0 ... indicating a QA finding" and was
        scored a miss because the rubric only listed "exceeds". Undercounting makes
        a capable agent look incapable, which is as damaging as overcounting.
        """
        entry = detection_module.load_rubric().faults["health_ratio_out_of_range"]
        found = "health_ratio was 1.25 and should not exceed 1.0, indicating a QA finding"

        self.assertNotEqual([], detection_module._terms_hit(found, entry.topic_terms))
        self.assertNotEqual([], detection_module._terms_hit(found, entry.symptom_terms))

    def test_computing_a_value_without_comparing_it_is_not_a_finding(self) -> None:
        """gpt-4o-mini computed 96/100 = 0.96 and called it valid, never comparing
        against the reported 1.25. Doing the arithmetic is not noticing."""
        entry = detection_module.load_rubric().faults["health_ratio_out_of_range"]
        computed = "Computed health_ratio is 96/100 = 0.96, which is valid"

        self.assertEqual([], detection_module._terms_hit(computed, entry.symptom_terms))

    def test_a_control_run_can_never_be_scored_match(self) -> None:
        """Regression lock across every rubric fault."""
        rubric = detection_module.load_rubric()
        for fault_id, entry in rubric.faults.items():
            text = " ".join([*entry.topic_terms, *entry.symptom_terms])
            with self.subTest(fault=fault_id):
                result = self.score(
                    fault_id=None,
                    transitions=[claim_step(0, "run", text, ["run-obs-00000003"])],
                    hypotheses=[{"candidate_id": "c-1", "statement": text, "status": "confirmed",
                                 "evidence_refs": ["run-obs-00000003"]}],
                    llm_assessment={"bug_candidates": [{"title": text}]},
                )
                self.assertNotEqual("match", result.status)
                self.assertEqual("false_positive", result.status)

    def test_a_silent_control_run_is_not_a_false_positive(self) -> None:
        result = self.score(fault_id=None, transitions=[claim_step(0, "run", "all normal", [])])

        self.assertEqual("not_evaluated", result.status)

    def test_an_exploratory_unexpected_alone_is_not_a_false_positive(self) -> None:
        """Change B makes raising `unexpected` cheap; punishing it would restore silence."""
        result = self.score(
            fault_id=None,
            transitions=[claim_step(0, "run", "health ratio looks inconsistent", ["run-obs-00000003"])],
        )

        self.assertEqual("not_evaluated", result.status)

    def test_heuristic_policy_is_never_scored(self) -> None:
        result = self.score(policy="heuristic", transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])])

        self.assertEqual("not_evaluated", result.status)

    def test_a_heuristic_run_without_an_inspector_is_never_scored(self) -> None:
        """Every existing heuristic artifact must stay ineligible for scoring."""
        result = self.score(policy="heuristic", transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])])

        self.assertEqual("not_evaluated", result.status)

    def test_a_heuristic_run_with_an_inspector_is_scored(self) -> None:
        """policy says who drove; the text channel says whether there is prose to judge."""
        found = self.score(
            policy="heuristic",
            has_agent_text_channel=True,
            transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])],
        )
        silent = self.score(policy="heuristic", has_agent_text_channel=True, transitions=[])

        self.assertEqual("match", found.status)
        self.assertEqual("miss", silent.status)

    def test_an_explicit_false_channel_overrides_an_llm_policy(self) -> None:
        result = self.score(
            policy="llm",
            has_agent_text_channel=False,
            transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])],
        )

        self.assertEqual("not_evaluated", result.status)

    def test_an_unobservable_fault_is_not_a_miss(self) -> None:
        result = self.score(fault_id="health_bar_desync", transitions=[])

        self.assertEqual("not_evaluated", result.status)
        self.assertIn("player_view", result.reason)

    def test_nothing_to_find_is_not_a_miss(self) -> None:
        for overrides in ({"oracle_verdict": "pass"}, {"fault_refs": []}):
            with self.subTest(**overrides):
                self.assertEqual("not_evaluated", self.score(**overrides).status)

    def test_a_partial_trace_cannot_be_a_miss_but_can_be_a_match(self) -> None:
        silent = self.score(trace_completeness="partial", transitions=[])
        found = self.score(
            trace_completeness="partial",
            transitions=[claim_step(0, "run", FINDING, ["run-obs-00000003"])],
        )

        self.assertEqual("not_evaluated", silent.status)
        self.assertEqual("match", found.status)

    def test_an_unknown_fault_is_not_a_miss(self) -> None:
        self.assertEqual("not_evaluated", self.score(fault_id="brand_new_fault").status)

    def test_the_assessment_surface_can_carry_a_match(self) -> None:
        ref = "a" * 32 + "-obs-00000003"
        result = self.score(
            fault_refs=[ref],
            llm_assessment={"bug_candidates": [{"title": FINDING, "evidence": ref}]},
        )

        self.assertEqual("match", result.status)
        self.assertEqual(["assessment"], result.matched_surfaces)

    def test_the_scorer_stays_out_of_the_human_annotation_protocol(self) -> None:
        """aggregate_annotations (3 reviewers, majority) stays the authority on validity."""
        source = (Path(__file__).resolve().parent / "detection.py").read_text(encoding="utf-8")
        # Mentioning the protocol in prose is the point; importing or writing it is not.
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]

        self.assertEqual([], [line for line in imports if "reporting" in line])
        for name in ("Annotation", "aggregate_annotations"):
            self.assertNotIn(name, "\n".join(imports))
        self.assertFalse(hasattr(detection_module, "Annotation"))
        self.assertNotIn("write_text", source)
        self.assertIn("sole authority", detection_module.AUTHORITY_NOTE)

    def test_scoring_never_raises_into_the_verdict_path(self) -> None:
        result = self.score(transitions=[{"decision": "not a dict"}], hypotheses=[{"status": 1}])

        self.assertIn(result.status, {"miss", "not_evaluated"})


class FaultEvidenceRefTests(unittest.TestCase):
    """Every observation the oracle objects to is citable evidence, not just the first."""

    def scenario(self) -> Scenario:
        return load_scenario("easy-health-ratio")

    def test_a_clean_trace_has_no_fault_refs(self) -> None:
        refs = fault_evidence_refs(self.scenario(), trace("run-clean", ratio_is_faulty=False))

        self.assertEqual([], refs)

    def test_every_violating_observation_is_collected(self) -> None:
        """evaluate_oracle stops at the first violation; an agent may notice later."""
        rows = trace("run-fault", ratio_is_faulty=True, count=6)

        refs = fault_evidence_refs(self.scenario(), rows)

        self.assertEqual(6, len(refs))
        self.assertIn("run-fault-obs-00000005", refs)
        self.assertEqual(1, len(evaluate_oracle(self.scenario(), rows).evidence_refs))

    def test_only_the_violating_tail_is_collected(self) -> None:
        rows = trace("run-mixed", ratio_is_faulty=False, count=3) + [
            health_step(i, "run-mixed", 98.0, 1.25) for i in range(3, 6)
        ]

        refs = fault_evidence_refs(self.scenario(), rows)

        self.assertEqual(
            ["run-mixed-obs-00000003", "run-mixed-obs-00000004", "run-mixed-obs-00000005"],
            refs,
        )

    def test_an_empty_trace_is_not_an_error(self) -> None:
        self.assertEqual([], fault_evidence_refs(self.scenario(), []))


class PartialTraceVerdictTests(unittest.TestCase):
    """An aborted run must surface faults it proved without claiming cleanliness."""

    def scenario_args(self) -> argparse.Namespace:
        return argparse.Namespace(scenario_definition=load_scenario("easy-health-ratio"))

    def recorder_with(self, name: str, steps: list[dict[str, object]]) -> RunRecorder:
        recorder = RunRecorder(TEST_TEMP_ROOT / name, "qa", "hybrid", 9102, model="gpt-4o-mini")
        recorder.steps = steps
        return recorder

    def test_aborted_run_reports_an_oracle_failure_from_its_partial_trace(self) -> None:
        recorder = self.recorder_with("partial-fail", trace("run-fault", ratio_is_faulty=True))

        verdict = run_module.build_session_verdict(
            self.scenario_args(), recorder, "RuntimeError: LLM API HTTP 429: rate limited", {}
        )

        self.assertEqual("infrastructure_error", verdict["execution_status"])
        self.assertEqual("reached", verdict["coverage_status"])
        self.assertEqual("fail", verdict["oracle_verdict"])
        self.assertEqual("partial", verdict["trace_completeness"])
        self.assertEqual("ERROR", verdict["final_verdict"])
        self.assertTrue(verdict["evidence_refs"])

    def test_aborted_run_cannot_report_an_oracle_pass(self) -> None:
        """The core trust lock: a prefix cannot establish that nothing went wrong."""
        recorder = self.recorder_with("partial-pass", trace("run-clean", ratio_is_faulty=False))

        verdict = run_module.build_session_verdict(
            self.scenario_args(), recorder, "RuntimeError: LLM API HTTP 429: rate limited", {}
        )

        self.assertEqual("reached", verdict["coverage_status"])
        self.assertEqual("not_evaluated", verdict["oracle_verdict"])
        self.assertEqual("partial", verdict["trace_completeness"])

    def test_contract_error_run_also_downgrades_a_partial_pass(self) -> None:
        recorder = self.recorder_with("contract-pass", trace("run-clean2", ratio_is_faulty=False))

        verdict = run_module.build_session_verdict(
            self.scenario_args(), recorder, "LLMContractError: invalid reflection", {}
        )

        self.assertEqual("contract_error", verdict["execution_status"])
        self.assertEqual("not_evaluated", verdict["oracle_verdict"])

    def test_completed_run_still_reports_a_pass(self) -> None:
        recorder = self.recorder_with("complete-pass", trace("run-clean3", ratio_is_faulty=False))

        verdict = run_module.build_session_verdict(self.scenario_args(), recorder, None, {})

        self.assertEqual("pass", verdict["oracle_verdict"])
        self.assertEqual("complete", verdict["trace_completeness"])
        self.assertEqual("PASS", verdict["final_verdict"])

    def test_empty_trace_falls_back_without_relabeling_execution_status(self) -> None:
        """A bridge crash must not be laundered into an agent contract error."""
        recorder = self.recorder_with("empty-trace", [])

        verdict = run_module.build_session_verdict(
            self.scenario_args(), recorder, "BridgeError: Game process exited with code -6", {}
        )

        self.assertEqual("infrastructure_error", verdict["execution_status"])
        self.assertEqual("not_reached", verdict["coverage_status"])
        self.assertEqual("not_evaluated", verdict["oracle_verdict"])

    def test_ad_hoc_run_without_a_scenario_is_never_evaluated(self) -> None:
        recorder = self.recorder_with("no-scenario", trace("run-fault2", ratio_is_faulty=True))

        verdict = run_module.build_session_verdict(
            argparse.Namespace(scenario_definition=None), recorder, None, {}
        )

        self.assertEqual("not_reached", verdict["coverage_status"])
        self.assertEqual("not_evaluated", verdict["oracle_verdict"])

    def test_prefixes_of_a_clean_trace_never_produce_a_false_oracle_failure(self) -> None:
        """Oracles pairing a before/after state must not fire on a truncated prefix."""
        scenario = load_scenario("easy-health-ratio")
        full = trace("run-prefix", ratio_is_faulty=False, count=8)
        for length in range(len(full) + 1):
            with self.subTest(prefix_length=length):
                axes = scenario_verdict_axes(scenario, full[:length], "completed")
                if axes is not None:
                    self.assertNotEqual("fail", axes.oracle_verdict)

    def test_deterministic_gate_still_rejects_a_non_completed_artifact(self) -> None:
        """Partial-trace verdicts must not open a loophole in the benchmark gate."""
        with self.assertRaisesRegex(ValueError, "infrastructure_error"):
            build_run_verdict(
                execution_status="infrastructure_error",
                coverage_status="reached",
                oracle_verdict="pass",
                agent_detection="not_evaluated",
                evidence_refs=["obs-1"],
                trace_completeness="partial",
            )

    def test_verdict_keeps_the_v2_schema(self) -> None:
        verdict = build_run_verdict(
            execution_status="infrastructure_error",
            coverage_status="reached",
            oracle_verdict="fail",
            agent_detection="not_evaluated",
            evidence_refs=["obs-1"],
            trace_completeness="partial",
        )

        self.assertEqual("qa-run-verdict/v2", verdict["schema_version"])
        self.assertEqual("ERROR", verdict["final_verdict"])

    def test_unsupported_trace_completeness_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "trace_completeness"):
            build_run_verdict(
                execution_status="completed",
                coverage_status="reached",
                oracle_verdict="pass",
                agent_detection="not_evaluated",
                evidence_refs=["obs-1"],
                trace_completeness="mostly",
            )


def rate_limited(retry_after: str | None = None, body: str = "{}"):
    """An opener that 429s a fixed number of times, then returns a valid plan."""

    def build(failures: int):
        state = {"calls": 0}

        def opener(*_args, **_kwargs):
            state["calls"] += 1
            if state["calls"] <= failures:
                headers = {"Retry-After": retry_after} if retry_after else {}
                raise urllib.error.HTTPError(
                    "https://example.invalid", 429, "Too Many Requests", headers,
                    io.BytesIO(body.encode("utf-8")),
                )
            return fake_opener(
                json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}], "usage": {}})
            )()

        return opener

    return build


class LLMRetryTests(unittest.TestCase):
    """Transient provider failures must not end an episode."""

    def planner(self, opener, sleeps: list[float], **kwargs) -> LLMPlanner:
        return LLMPlanner(
            "qa", "test-model", TestCharter(), 5.0,
            api_key="test-key", urlopen=opener, sleep=sleeps.append, **kwargs
        )

    def send(self, planner: LLMPlanner):
        return planner._send_with_retries(urllib.request.Request("https://example.invalid"))

    def test_rate_limited_request_retries_then_succeeds(self) -> None:
        sleeps: list[float] = []
        planner = self.planner(rate_limited()(2), sleeps)

        body = self.send(planner)

        self.assertTrue(body["choices"])
        self.assertEqual([1.0, 2.0], sleeps)
        self.assertEqual(2, planner._retry_count)
        self.assertEqual(3, planner._http_attempts)

    def test_retry_after_header_overrides_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        self.send(self.planner(rate_limited(retry_after="11.169")(1), sleeps))

        self.assertEqual([11.169], sleeps)

    def test_retry_delay_is_recovered_from_the_error_body(self) -> None:
        """Every real 429 this harness recorded carried the delay only in the text."""
        sleeps: list[float] = []
        body = '{"error": {"message": "Rate limit reached. Please try again in 1.531s."}}'
        self.send(self.planner(rate_limited(body=body)(1), sleeps))

        self.assertEqual([1.531], sleeps)

    def test_a_single_absurd_hint_cannot_stall_the_run(self) -> None:
        sleeps: list[float] = []
        self.send(self.planner(rate_limited(retry_after="600")(1), sleeps))

        self.assertEqual([20.0], sleeps)

    def test_retry_budget_bounds_total_added_wall_time(self) -> None:
        sleeps: list[float] = []
        planner = self.planner(
            rate_limited(retry_after="4")(5), sleeps, max_attempts=5, retry_budget_seconds=5.0
        )

        with self.assertRaisesRegex(planners_module.LLMTransportError, "budget"):
            self.send(planner)
        self.assertEqual([4.0], sleeps)

    def test_authentication_failure_is_not_retried(self) -> None:
        """Quota lock: a bad key must fail fast, not burn attempts."""
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"{}")
            )

        with self.assertRaises(planners_module.LLMTransportError):
            self.send(self.planner(opener, sleeps))
        self.assertEqual([], sleeps)

    def test_bad_request_is_not_retried(self) -> None:
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "Bad Request", {}, io.BytesIO(b"{}")
            )

        with self.assertRaises(planners_module.LLMTransportError):
            self.send(self.planner(opener, sleeps))
        self.assertEqual([], sleeps)

    def test_max_attempts_one_restores_the_previous_behavior(self) -> None:
        sleeps: list[float] = []
        planner = self.planner(rate_limited()(1), sleeps, max_attempts=1)

        with self.assertRaises(planners_module.LLMTransportError):
            self.send(planner)
        self.assertEqual([], sleeps)

    def test_exhaustion_message_records_the_attempts(self) -> None:
        sleeps: list[float] = []

        with self.assertRaisesRegex(planners_module.LLMTransportError, r"attempts=3.*retries=2"):
            self.send(self.planner(rate_limited()(9), sleeps))

    def test_retry_counters_reach_api_usage_totals(self) -> None:
        recorder = RunRecorder(TEST_TEMP_ROOT / "retry-usage", "qa", "llm", 9101)
        for _ in range(2):
            recorder.add_api_usage(
                "planning_request",
                {"request_count": 1, "llm_retries": 2, "llm_http_attempts": 3, "llm_retry_wait_ms": 3000},
            )

        totals = recorder.api_usage_totals()

        self.assertEqual(4, totals["llm_retries"])
        self.assertEqual(6, totals["llm_http_attempts"])
        self.assertEqual(6000, totals["llm_retry_wait_ms"])
        self.assertEqual(2, totals["calls"])

    def test_retry_limits_are_exposed_on_the_cli(self) -> None:
        args = run_module.parse_args(
            [
                "--game-exe", "player.app",
                "--output", "artifacts",
                "--llm-max-attempts", "5",
                "--llm-retry-budget-seconds", "10.5",
            ]
        )

        self.assertEqual(5, args.llm_max_attempts)
        self.assertEqual(10.5, args.llm_retry_budget_seconds)

    def test_retry_limits_do_not_conflict_with_a_scenario_charter(self) -> None:
        """They are infrastructure knobs, so a scenario run may still set them."""
        args = run_module.parse_args(
            [
                "--game-exe", "player.app",
                "--output", "artifacts",
                "--scenario", "easy-health-ratio",
                "--seed", "9102",
                "--llm-max-attempts", "1",
            ]
        )

        self.assertEqual(1, args.llm_max_attempts)

    def test_invalid_retry_configuration_is_rejected(self) -> None:
        for kwargs in ({"max_attempts": 0}, {"retry_budget_seconds": -1.0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    LLMPlanner("qa", "m", TestCharter(), 5.0, api_key="k", **kwargs)


def completion(content: str = "{}", finish_reason: str = "stop", tokens: int = 10) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"total_tokens": tokens, "completion_tokens": tokens},
        }
    )


class ReasoningModelTests(unittest.TestCase):
    """Reasoning models reject max_tokens and spend budget before emitting output."""

    def body_for(
        self,
        model: str,
        responses: list[str] | None = None,
        response_schema: dict | None = None,
        **kwargs,
    ) -> list[dict]:
        bodies: list[dict] = []

        def opener(request, *_args, **_kw):
            bodies.append(json.loads(request.data.decode("utf-8")))
            payload = (responses or [completion('{"a": 1}')])[min(len(bodies) - 1, len(responses or [1]) - 1)]
            return fake_opener(payload)()

        LLMPlanner(
            "qa", model, TestCharter(), 5.0, api_key="test-key", urlopen=opener, **kwargs
        )._request("sys", "user", response_schema=response_schema)
        return bodies

    def test_a_standard_model_still_uses_max_tokens(self) -> None:
        body = self.body_for("gpt-4o-mini")[0]

        self.assertEqual(planners_module.PLANNING_MAX_TOKENS, body["max_tokens"])
        self.assertNotIn("max_completion_tokens", body)
        self.assertNotIn("reasoning_effort", body)

    def test_a_reasoning_model_uses_max_completion_tokens(self) -> None:
        body = self.body_for("gpt-5.6-luna")[0]

        self.assertNotIn("max_tokens", body)
        self.assertEqual(planners_module.REASONING_OUTPUT_FLOOR, body["max_completion_tokens"])

    def test_reasoning_effort_is_sent_when_requested(self) -> None:
        body = self.body_for("gpt-5.6-luna", reasoning_effort="low")[0]

        self.assertEqual("low", body["reasoning_effort"])

    def test_the_truncation_retry_keeps_the_reasoning_budget_key(self) -> None:
        """The retry loop rewrites the budget, and used to reintroduce max_tokens."""
        bodies = self.body_for(
            "gpt-5.6-luna",
            responses=[completion(finish_reason="length", tokens=8000), completion('{"a": 1}')],
        )

        self.assertEqual([8000, 16000], [b["max_completion_tokens"] for b in bodies])
        self.assertTrue(all("max_tokens" not in b for b in bodies))

    def test_a_reasoning_model_still_requests_strict_structured_output(self) -> None:
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        body = self.body_for("gpt-5.6-luna", response_schema=schema)[0]

        self.assertEqual("json_schema", body["response_format"]["type"])

    def test_an_unsupported_reasoning_effort_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            LLMPlanner("qa", "gpt-5.6-luna", TestCharter(), 5.0, api_key="k", reasoning_effort="turbo")


class TruncationTests(unittest.TestCase):
    """Running out of output room must cost a retry, not the episode."""

    def planner_capturing(self, bodies: list[dict[str, object]], responses: list[str]):
        def opener(request, *_args, **_kwargs):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return fake_opener(responses[min(len(bodies) - 1, len(responses) - 1)])()

        return LLMPlanner(
            "qa", "test-model", TestCharter(), 5.0, api_key="test-key", urlopen=opener
        )

    def test_planning_requests_use_the_raised_output_cap(self) -> None:
        bodies: list[dict[str, object]] = []
        planner = self.planner_capturing(bodies, [completion('{"a": 1}')])

        planner._request("sys", "user")

        self.assertEqual(planners_module.PLANNING_MAX_TOKENS, bodies[0]["max_tokens"])
        self.assertEqual(700, planners_module.PLANNING_MAX_TOKENS)

    def test_a_truncated_response_is_retried_once_at_a_higher_cap(self) -> None:
        bodies: list[dict[str, object]] = []
        planner = self.planner_capturing(
            bodies,
            [completion(finish_reason="length", tokens=700), completion('{"a": 1}')],
        )

        result = planner._request("sys", "user")

        self.assertEqual({"a": 1}, result)
        self.assertEqual([700, 1400], [body["max_tokens"] for body in bodies])

    def test_repeated_truncation_raises_naming_both_caps(self) -> None:
        bodies: list[dict[str, object]] = []
        planner = self.planner_capturing(
            bodies, [completion(finish_reason="length", tokens=700)]
        )

        with self.assertRaisesRegex(planners_module.LLMTruncationError, r"\[700, 1400\]"):
            planner._request("sys", "user")

    def test_tokens_from_truncated_attempts_are_still_reported(self) -> None:
        bodies: list[dict[str, object]] = []
        planner = self.planner_capturing(
            bodies, [completion(finish_reason="length", tokens=700)]
        )

        with self.assertRaises(planners_module.LLMTruncationError):
            planner._request("sys", "user")

        usage = planner.take_last_usage()
        self.assertEqual(1400, usage["total_tokens"])
        self.assertEqual(1, usage["request_count"])

    def test_retries_across_truncation_completions_are_aggregated(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls in {1, 3}:
                raise urllib.error.HTTPError(
                    "https://example.invalid",
                    429,
                    "Too Many Requests",
                    {},
                    io.BytesIO(b"{}"),
                )
            payload = (
                completion(finish_reason="length", tokens=700)
                if calls == 2
                else completion('{"a": 1}', tokens=10)
            )
            return fake_opener(payload)()

        planner = LLMPlanner(
            "qa",
            "test-model",
            TestCharter(),
            5.0,
            api_key="test-key",
            urlopen=opener,
            sleep=sleeps.append,
        )

        result = planner._request("sys", "user")

        usage = planner.take_last_usage()
        self.assertEqual({"a": 1}, result)
        self.assertEqual(1, usage["request_count"])
        self.assertEqual(2, usage["llm_completion_requests"])
        self.assertEqual(4, usage["llm_http_attempts"])
        self.assertEqual(2, usage["llm_retries"])
        self.assertEqual(2000, usage["llm_retry_wait_ms"])
        self.assertEqual([1.0, 1.0], sleeps)

    def test_transport_only_failure_still_reports_all_http_attempts(self) -> None:
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://example.invalid",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b"{}"),
            )

        planner = LLMPlanner(
            "qa",
            "test-model",
            TestCharter(),
            5.0,
            api_key="test-key",
            urlopen=opener,
            sleep=sleeps.append,
        )

        with self.assertRaises(planners_module.LLMTransportError):
            planner._request("sys", "user")

        usage = planner.take_last_usage()
        self.assertEqual(1, usage["request_count"])
        self.assertEqual(1, usage["llm_completion_requests"])
        self.assertEqual(3, usage["llm_http_attempts"])
        self.assertEqual(2, usage["llm_retries"])
        self.assertEqual(3000, usage["llm_retry_wait_ms"])
        self.assertEqual([1.0, 2.0], sleeps)

    def test_the_repair_path_uses_the_same_cap(self) -> None:
        bodies: list[dict[str, object]] = []
        planner = self.planner_capturing(bodies, [completion('{"a": 1}')])
        planner._pending_user_content = "prior"

        planner._request("sys", "user", max_tokens=planners_module.PLANNING_MAX_TOKENS)

        self.assertEqual(700, bodies[0]["max_tokens"])


class PlannerModelContentTests(unittest.TestCase):
    """Parsing failures expose only the model content needed for failure audits."""

    def test_non_json_model_content_is_preserved_and_drained_without_request_metadata(self) -> None:
        response = json.dumps(
            {
                "id": "provider-envelope-metadata",
                "model": "provider-model-metadata",
                "choices": [
                    {
                        "message": {"content": "plain model output"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        )
        planner = LLMPlanner(
            "qa",
            "test-model",
            TestCharter(),
            5.0,
            api_key="request-header-secret",
            urlopen=fake_opener(response),
        )

        with self.assertRaises(json.JSONDecodeError):
            planner._request("private system prompt", "private user prompt")

        self.assertEqual("plain model output", planner.take_last_model_content())
        self.assertIsNone(planner.take_last_model_content())

    def test_json_non_object_content_is_preserved_and_the_next_request_resets_it(self) -> None:
        responses = iter(
            [
                completion('["array item"]'),
                completion("stale model output"),
            ]
        )

        def opener(*_args, **_kwargs):
            try:
                return fake_opener(next(responses))()
            except StopIteration:
                raise urllib.error.URLError("provider unavailable") from None

        planner = LLMPlanner(
            "qa",
            "test-model",
            TestCharter(),
            5.0,
            api_key="test-key",
            urlopen=opener,
            max_attempts=1,
        )

        with self.assertRaisesRegex(ValueError, "JSON object"):
            planner._request("system", "user")
        self.assertEqual('["array item"]', planner.take_last_model_content())

        with self.assertRaises(json.JSONDecodeError):
            planner._request("system", "user")
        with self.assertRaises(planners_module.LLMTransportError):
            planner._request("system", "user")

        self.assertIsNone(planner.take_last_model_content())


class PlannerUsageAccountingTests(unittest.TestCase):
    """Tokens the provider billed must be recorded even when the call fails."""

    class FailingPlanner:
        """Sets usage the way _request does, then raises like truncation does."""

        def __init__(self) -> None:
            self.last_usage = {
                "prompt_tokens": 900,
                "completion_tokens": 300,
                "total_tokens": 1200,
                "cache_boundary": 0,
            }

        def take_last_usage(self) -> dict[str, object]:
            usage, self.last_usage = self.last_usage, {}
            return usage

    def test_usage_from_a_failed_call_is_still_recorded(self) -> None:
        recorder = RunRecorder(TEST_TEMP_ROOT / "usage-drain", "qa", "llm", 9101)
        planner = self.FailingPlanner()

        run_module.drain_planner_usage(recorder, planner, "planning_request", 3)

        totals = recorder.api_usage_totals()
        self.assertEqual(1200, totals["total_tokens"])
        self.assertEqual(1, len(recorder.api_usage_events))

    def test_draining_twice_does_not_double_count(self) -> None:
        recorder = RunRecorder(TEST_TEMP_ROOT / "usage-drain-twice", "qa", "llm", 9101)
        planner = self.FailingPlanner()

        run_module.drain_planner_usage(recorder, planner, "planning_request", 3)
        run_module.drain_planner_usage(recorder, planner, "planning_request", 4)

        self.assertEqual(1200, recorder.api_usage_totals()["total_tokens"])
        self.assertEqual(1, len(recorder.api_usage_events))

    def test_a_heuristic_planner_records_nothing(self) -> None:
        recorder = RunRecorder(TEST_TEMP_ROOT / "usage-drain-heuristic", "qa", "heuristic", 9101)

        run_module.drain_planner_usage(
            recorder, HeuristicPlanner(5.0, TestCharter()), "planning_request", 0
        )

        self.assertEqual([], recorder.api_usage_events)

    def test_failed_request_usage_does_not_leak_into_the_next_call(self) -> None:
        """last_usage is only cleared by take_last_usage, so an undrained failure
        would otherwise be attributed to whichever call succeeded next."""
        state = {"calls": 0}

        def opener(*_args, **_kwargs):
            state["calls"] += 1
            # The first request truncates on both attempts and dies; the next
            # one succeeds. Its usage must not carry the dead request's tokens.
            failing = state["calls"] <= 2
            return fake_opener(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": "{}"},
                                "finish_reason": "length" if failing else "stop",
                            }
                        ],
                        "usage": {"total_tokens": 999 if failing else 10},
                    }
                )
            )()

        planner = LLMPlanner(
            "qa", "test-model", TestCharter(), 5.0, api_key="test-key", urlopen=opener
        )
        with self.assertRaises(planners_module.LLMTruncationError):
            planner._request("sys", "user")
        self.assertEqual(1998, planner.take_last_usage()["total_tokens"])

        planner._request("sys", "user")

        self.assertEqual(10, planner.take_last_usage()["total_tokens"])


class LLMTransportLabelTests(unittest.TestCase):
    """Every way a request can fail must be labelled and stay infrastructure."""

    def send(self, opener) -> None:
        planner = LLMPlanner(
            "qa", "test-model", TestCharter(), 5.0, api_key="test-key", urlopen=opener
        )
        planner._send_once(urllib.request.Request("https://example.invalid"))

    def test_http_error_is_labelled_a_transport_error(self) -> None:
        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "Too Many Requests", {},
                io.BytesIO(b'{"error": {"message": "rate limited"}}'),
            )

        with self.assertRaisesRegex(planners_module.LLMTransportError, "HTTP 429"):
            self.send(opener)

    def test_connection_error_is_labelled_a_transport_error(self) -> None:
        def opener(*_args, **_kwargs):
            raise urllib.error.URLError("connection reset")

        with self.assertRaises(planners_module.LLMTransportError):
            self.send(opener)

    def test_timeout_is_labelled_a_transport_error(self) -> None:
        def opener(*_args, **_kwargs):
            raise TimeoutError()

        with self.assertRaisesRegex(planners_module.LLMTransportError, "timed out"):
            self.send(opener)

    def test_non_json_body_is_labelled_a_response_error(self) -> None:
        with self.assertRaisesRegex(planners_module.LLMResponseError, "non-JSON"):
            self.send(fake_opener("<html>gateway</html>"))

    def test_envelope_without_choices_is_labelled_a_response_error(self) -> None:
        with self.assertRaisesRegex(planners_module.LLMResponseError, "no choices"):
            self.send(fake_opener(json.dumps({"usage": {}})))

    def test_transport_failures_are_classified_as_infrastructure_errors(self) -> None:
        """Guards the name-prefix coupling in build_session_verdict.

        A provider outage must never be recorded as an agent contract violation.
        """
        recorder = RunRecorder(TEST_TEMP_ROOT / "transport-verdict", "qa", "llm", 9101)
        for name in ("LLMTransportError", "LLMResponseError"):
            with self.subTest(error=name):
                verdict = run_module.build_session_verdict(
                    argparse.Namespace(scenario_definition=None),
                    recorder,
                    f"{name}: provider is unavailable",
                    {},
                )
                self.assertEqual("infrastructure_error", verdict["execution_status"])


class ReevaluateTests(unittest.TestCase):
    """Recovering verdicts from stored artifacts must not invent findings."""

    def write_run(
        self,
        name: str,
        *,
        run_id: str = "run-a",
        execution_status: str = "infrastructure_error",
        rows: list[dict[str, object]] | None = None,
        scenario_id: str = "easy-health-ratio",
    ) -> Path:
        run_dir = TEST_TEMP_ROOT / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "scenario_id": scenario_id}), encoding="utf-8"
        )
        (run_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "qa-run-verdict/v2",
                    "execution_status": execution_status,
                    "coverage_status": "not_reached",
                    "oracle_verdict": "not_evaluated",
                    "agent_detection": "not_evaluated",
                    "evidence_refs": [],
                    "final_verdict": "ERROR",
                }
            ),
            encoding="utf-8",
        )
        payload = rows if rows is not None else trace(run_id, ratio_is_faulty=True)
        (run_dir / "steps.jsonl").write_text(
            "".join(json.dumps({**row, "run_id": run_id}) + "\n" for row in payload),
            encoding="utf-8",
        )
        return run_dir

    def test_aborted_fault_run_recovers_its_failure(self) -> None:
        run_dir = self.write_run("reeval-fault")

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertEqual("fail", updated["oracle_verdict"])
        self.assertEqual("reached", updated["coverage_status"])
        self.assertEqual("partial", updated["trace_completeness"])
        self.assertEqual("ERROR", updated["final_verdict"])

    def test_execution_status_is_never_revised(self) -> None:
        run_dir = self.write_run("reeval-status", execution_status="contract_error")

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertEqual("contract_error", updated["execution_status"])

    def test_aborted_clean_run_is_not_promoted_to_pass(self) -> None:
        run_dir = self.write_run(
            "reeval-clean", rows=trace("run-a", ratio_is_faulty=False)
        )

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertEqual("not_evaluated", updated["oracle_verdict"])

    def test_rows_from_another_session_are_ignored(self) -> None:
        """steps.jsonl is append-mode, so a reused directory holds several runs.

        Judging the union let a fault-injected session bleed into a fault-free
        one and produced a control false positive.
        """
        run_dir = self.write_run(
            "reeval-mixed", run_id="run-clean", rows=trace("run-clean", ratio_is_faulty=False)
        )
        with (run_dir / "steps.jsonl").open("a", encoding="utf-8") as handle:
            for row in trace("run-faulty", ratio_is_faulty=True):
                handle.write(json.dumps({**row, "run_id": "run-faulty"}) + "\n")

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertNotEqual("fail", updated["oracle_verdict"])

    def test_empty_trace_is_skipped(self) -> None:
        run_dir = self.write_run("reeval-empty", rows=[])

        with self.assertRaises(reevaluate_module.ReevaluationSkipped):
            reevaluate_module.reevaluate_run(run_dir)

    def test_unknown_scenario_is_skipped(self) -> None:
        run_dir = self.write_run("reeval-unknown", scenario_id="no-such-scenario")

        with self.assertRaises(reevaluate_module.ReevaluationSkipped):
            reevaluate_module.reevaluate_run(run_dir)

    def test_rewrite_is_idempotent(self) -> None:
        run_dir = self.write_run("reeval-idempotent")

        reevaluate_module.main([str(run_dir)])
        first = (run_dir / "verdict.json").read_text(encoding="utf-8")
        reevaluate_module.main([str(run_dir)])

        self.assertEqual(first, (run_dir / "verdict.json").read_text(encoding="utf-8"))

    def test_stored_runs_are_scored_for_agent_detection(self) -> None:
        run_dir = self.write_run("reeval-detect", execution_status="completed")
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run-a",
                    "scenario_id": "easy-health-ratio",
                    "policy": "llm",
                    "fault_id": "health_ratio_out_of_range",
                }
            ),
            encoding="utf-8",
        )

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertEqual("miss", updated["agent_detection"])
        self.assertIn("_detection_payload", updated)

    def test_a_v4_scenario_id_resolves_through_its_legacy_scenario(self) -> None:
        scenario = reevaluate_module.resolve_scenario("easy-hp-on-hit")

        self.assertIsNotNone(scenario)

    def test_scoring_uses_only_this_runs_rows(self) -> None:
        """A fault session concatenated into a control run must not score it."""
        run_dir = self.write_run(
            "reeval-detect-mixed",
            run_id="run-clean",
            execution_status="completed",
            rows=trace("run-clean", ratio_is_faulty=False),
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {"run_id": "run-clean", "scenario_id": "easy-health-ratio", "policy": "llm", "fault_id": None}
            ),
            encoding="utf-8",
        )
        with (run_dir / "steps.jsonl").open("a", encoding="utf-8") as handle:
            for row in trace("run-faulty", ratio_is_faulty=True):
                handle.write(json.dumps({**row, "run_id": "run-faulty"}) + "\n")

        updated = reevaluate_module.reevaluate_run(run_dir)

        self.assertNotEqual("false_positive", updated["agent_detection"])

    def test_dry_run_writes_nothing(self) -> None:
        run_dir = self.write_run("reeval-dry")
        before = (run_dir / "verdict.json").read_text(encoding="utf-8")

        reevaluate_module.main(["--dry-run", str(run_dir)])

        self.assertEqual(before, (run_dir / "verdict.json").read_text(encoding="utf-8"))


class InspectionWireSchemaTests(unittest.TestCase):
    """The inspector schema travels to Structured Outputs, which rejects oneOf.

    Pydantic serialises the discriminated finding union as oneOf plus a
    discriminator, and the endpoint answers HTTP 400 for both. Every inspection
    call then fails as a transport error, which reads like an outage rather
    than a contract break, so the wire schema is pinned here.
    """

    def _walk(self, node, path=()):
        if isinstance(node, dict):
            for key, value in node.items():
                yield path + (str(key),), key, value
                yield from self._walk(value, path + (str(key),))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from self._walk(value, path + (str(index),))

    def test_findings_schema_uses_no_keyword_structured_outputs_rejects(self) -> None:
        from qa_smoke.inspector import FINDINGS_SCHEMA

        rejected = {
            path: key
            for path, key, _ in self._walk(FINDINGS_SCHEMA)
            if key in ("oneOf", "discriminator")
        }

        self.assertEqual({}, rejected)

    def test_findings_schema_keeps_both_finding_variants(self) -> None:
        from qa_smoke.inspector import FINDINGS_SCHEMA

        items = FINDINGS_SCHEMA["properties"]["findings"]["items"]

        self.assertEqual(
            [{"$ref": "#/$defs/NumericFinding"}, {"$ref": "#/$defs/BehaviorFinding"}],
            items["anyOf"],
        )

    def test_findings_schema_root_is_an_object_not_a_union(self) -> None:
        """Structured Outputs rejects a root that evaluates to anyOf."""
        from qa_smoke.inspector import FINDINGS_SCHEMA

        self.assertEqual("object", FINDINGS_SCHEMA["type"])
        self.assertNotIn("anyOf", FINDINGS_SCHEMA)

    def test_both_finding_variants_still_validate(self) -> None:
        """The wire schema change must not loosen server-side validation."""
        from qa_smoke.inspector import validate_inspection_artifact_v2

        artifact = validate_inspection_artifact_v2(
            {
                "schema_version": "qa-inspection/v2",
                "findings": [
                    {
                        "kind": "numeric",
                        "field": "player.health",
                        "comparison": "==",
                        "expected_value": 100.0,
                        "observed_value": 80.0,
                        "statement": "health disagreed with the bar",
                        "evidence_refs": ["obs-1"],
                    },
                    {
                        "kind": "behavior",
                        "rule": "restart clears inventory",
                        "expected_value": "empty",
                        "observed_value": "one potion",
                        "statement": "inventory survived a restart",
                        "evidence_refs": ["obs-2"],
                    },
                ],
            }
        )

        self.assertEqual(["numeric", "behavior"], [f["kind"] for f in artifact["findings"]])


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

    def test_the_prompt_asks_for_single_observation_invariant_checks(self) -> None:
        """The agent scored `matched` 17 times because that was the question asked.

        Nothing in the prompt used to mention consistency, range or invariants, so a
        correct action with a corrupt observation was legitimately "as expected".
        """
        for bridge_assist in (False, True):
            with self.subTest(bridge_assist=bridge_assist):
                planner = LLMPlanner(
                    "qa", "test-model", TestCharter(bridge_assist=bridge_assist), 5.0,
                    api_key="test-key",
                )
                prompt = planner._planning_system_prompt()

                self.assertIn(
                    "a matched transition says nothing about whether the state itself is valid",
                    prompt,
                )
                # The forcing mechanism: the agent must compute, not just assert.
                self.assertIn("actually compute it before you answer", prompt)
                self.assertIn("the value you computed from them", prompt)
                self.assertIn("reflection.status=unexpected", prompt)
                # Invariant classes only: no field, value or subsystem is named.
                self.assertNotIn("health_ratio", prompt)
                self.assertNotIn("1.25", prompt)

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

    def test_llm_policy_never_requests_bridge_assist(self) -> None:
        """The regression lock: --policy llm must stay a pure raw-vector arm."""
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5},
            },
            "qa",
            TestCharter(),
            llm_direct_control=True,
        )

        self.assertFalse(decision["arguments"]["assist_avoidance"])
        self.assertEqual(0.0, decision["arguments"]["assist_survival_weight"])

    def test_hybrid_policy_injects_bridge_assist_into_direct_steer(self) -> None:
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 0.6, "y": -0.8, "duration": 5},
            },
            "qa",
            TestCharter(bridge_assist=True, assist_survival_weight=0.6),
            llm_direct_control=True,
        )

        self.assertTrue(decision["arguments"]["assist_avoidance"])
        self.assertEqual(0.6, decision["arguments"]["assist_survival_weight"])
        # The bridge blends; it must never rewrite the requested vector host-side.
        self.assertEqual(0.6, decision["arguments"]["x"])
        self.assertEqual(-0.8, decision["arguments"]["y"])

    def test_hybrid_active_gameplay_exposes_only_ready_inventory_items(self) -> None:
        observation = {
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["direct_steer", "use_item"],
            "inventory": {
                "slots": [
                    {"index": 0, "type": "Health", "count": 1, "pending_count": 0},
                    {"index": 1, "type": "Bomb", "count": 0, "pending_count": 2},
                    {"index": 2, "type": "Magnet", "count": 3, "pending_count": 0},
                ]
            },
        }

        contract = build_action_contract(
            observation, "qa", TestCharter(bridge_assist=True), 0
        )

        self.assertEqual(
            ["game.direct_steer", "game.use_item"], contract["allowed_calls"]
        )
        self.assertEqual([0, 2], contract["allowed_indices"])
        self.assertEqual(
            {"index": "integer chosen from ready_item_indices=[0, 2]"},
            contract["argument_contracts"]["game.use_item"],
        )

    def test_hybrid_ready_item_contract_reason_permits_item_use(self) -> None:
        contract = build_action_contract(
            {
                "player": {"present": True, "alive": True},
                "menu": {},
                "available_actions": ["direct_steer", "use_item"],
                "inventory": {
                    "slots": [{"index": 0, "type": "Health", "count": 1}]
                },
            },
            "qa",
            TestCharter(bridge_assist=True),
            0,
        )

        self.assertIn("game.use_item", contract["allowed_calls"])
        self.assertIn("use_item", contract["reason"])
        self.assertNotIn("next movement vector", contract["reason"])

    def test_hybrid_item_only_contract_reason_does_not_offer_movement(self) -> None:
        contract = build_action_contract(
            {
                "player": {"present": True, "alive": True},
                "menu": {},
                "available_actions": ["use_item"],
                "inventory": {
                    "slots": [{"index": 0, "type": "Health", "count": 1}]
                },
            },
            "qa",
            TestCharter(bridge_assist=True),
            0,
        )

        self.assertEqual(["game.use_item"], contract["allowed_calls"])
        self.assertIn("game.use_item", contract["reason"])
        self.assertNotIn("permitted movement action", contract["reason"])

    def test_hybrid_item_decision_uses_the_selected_call_contract(self) -> None:
        observation = {
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["direct_steer", "use_item"],
            "inventory": {"slots": [{"index": 4, "type": "Health", "count": 1}]},
        }
        planner = LLMPlanner(
            "qa", "test-model", TestCharter(bridge_assist=True), 5.0, api_key="test-key"
        )
        contract = planner._planning_payload(observation, 1, [], 0)["action_contract"]
        item_decision = {
            "qa_observation": "Health is low enough to justify consuming the ready item.",
            "tool": "game",
            "action": "use_item",
            "arguments": {"index": 4},
            "expected_effect": "The ready item should be consumed and affect the player.",
            "reflection": {
                "status": "not_applicable",
                "summary": "No previous transition exists.",
                "evidence_refs": [],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        self.assertEqual(
            {"index": "integer chosen from ready_item_indices=[4]"},
            contract["argument_contracts"]["game.use_item"],
        )
        self.assertIsNone(validate_decision_against_contract(item_decision, contract))

    def test_llm_active_gameplay_exposes_ready_inventory_items(self) -> None:
        """Track A and Track B autonomous episodes run --policy llm.

        Gating use_item on bridge_assist made item faults unreachable there, so the
        item surface must not depend on the assist.
        """
        observation = {
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["direct_steer", "use_item"],
            "inventory": {
                "slots": [
                    {"index": 0, "type": "Health", "count": 1, "pending_count": 0},
                    {"index": 1, "type": "Bomb", "count": 0, "pending_count": 2},
                ]
            },
        }

        contract = build_action_contract(observation, "qa", TestCharter(), 0)

        self.assertEqual(
            ["game.direct_steer", "game.use_item"], contract["allowed_calls"]
        )
        self.assertEqual([0], contract["allowed_indices"])
        self.assertEqual(
            {"index": "integer chosen from ready_item_indices=[0]"},
            contract["argument_contracts"]["game.use_item"],
        )

    def test_llm_active_gameplay_without_ready_items_stays_movement_only(self) -> None:
        observation = {
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["direct_steer", "use_item"],
            "inventory": {"slots": [{"index": 0, "type": "Bomb", "count": 0}]},
        }

        contract = build_action_contract(observation, "qa", TestCharter(), 0)

        self.assertEqual(["game.direct_steer"], contract["allowed_calls"])
        self.assertEqual([], contract["allowed_indices"])
        self.assertNotIn("argument_contracts", contract)

    def test_bridge_assist_is_not_recorded_as_a_constraint_enforcement(self) -> None:
        """constraint_enforcements means the agent violated the charter.

        The assist is declared bridge behavior, so logging it there would both
        inflate a headline A/B metric and overload the field's meaning.
        """
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5},
            },
            "qa",
            TestCharter(bridge_assist=True),
            llm_direct_control=True,
        )

        self.assertEqual([], decision["constraint_enforcements"])

    def test_hybrid_high_danger_caps_movement_horizon_and_records_adjustment(self) -> None:
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
            },
            "qa",
            TestCharter(bridge_assist=True),
            action_seconds=5.0,
            llm_direct_control=True,
            observation={"world": {"danger_score": 0.85}},
        )

        self.assertEqual(1.0, decision["arguments"]["duration"])
        self.assertEqual(
            [
                {
                    "kind": "hybrid_danger_horizon_cap",
                    "danger_score": 0.85,
                    "threshold": 0.85,
                    "requested_duration": 5.0,
                    "executed_duration": 1.0,
                }
            ],
            decision["policy_adjustments"],
        )

    def test_hybrid_safe_state_keeps_configured_movement_horizon(self) -> None:
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
            },
            "qa",
            TestCharter(bridge_assist=True),
            action_seconds=5.0,
            llm_direct_control=True,
            observation={"world": {"danger_score": 0.84}},
        )

        self.assertEqual(5.0, decision["arguments"]["duration"])
        self.assertNotIn("policy_adjustments", decision)

    def test_llm_high_danger_keeps_pure_policy_movement_horizon(self) -> None:
        decision = normalize_decision(
            {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
            },
            "qa",
            TestCharter(),
            action_seconds=5.0,
            llm_direct_control=True,
            observation={"world": {"danger_score": 0.95}},
        )

        self.assertEqual(5.0, decision["arguments"]["duration"])
        self.assertNotIn("policy_adjustments", decision)

    def test_hybrid_policy_selects_the_llm_planner(self) -> None:
        args = run_module.parse_args(
            [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--mode", "qa",
                "--policy", "hybrid",
            ]
        )

        self.assertTrue(args.uses_llm_planner)
        self.assertTrue(args.bridge_assist)
        self.assertTrue(run_module.charter_from_args(args).bridge_assist)

    def test_llm_policy_does_not_enable_the_bridge_assist(self) -> None:
        args = run_module.parse_args(
            [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--mode", "qa",
                "--policy", "llm",
            ]
        )

        self.assertTrue(args.uses_llm_planner)
        self.assertFalse(args.bridge_assist)
        self.assertFalse(run_module.charter_from_args(args).bridge_assist)

    def test_assist_weight_is_rejected_without_the_hybrid_policy(self) -> None:
        with self.assertRaisesRegex(ScenarioContractError, "only applies to --policy hybrid"):
            run_module.parse_args(
                [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--policy", "llm",
                    "--assist-survival-weight", "0.9",
                ]
            )

    def test_scenario_run_accepts_the_hybrid_policy(self) -> None:
        """--policy is not scenario-owned, so it must not trip the charter conflict."""
        args = run_module.parse_args(
            [
                    "--game-exe", "player.app",
                    "--output", "artifacts",
                    "--project-root", str(Path(__file__).resolve().parent.parent),
                    "--mode", "qa",
                    "--policy", "hybrid",
                    "--scenario", "easy-health-ratio",
                "--seed", "9102",
            ]
        )

        self.assertTrue(args.bridge_assist)
        self.assertEqual(9102, args.seed)
        self.assertTrue(run_module.charter_from_args(args).as_dict()["control_policy"]["automatic_enemy_avoidance"])

    def test_recorder_aggregates_bridge_assist_metrics(self) -> None:
        recorder = RunRecorder(
            output_dir=TEST_TEMP_ROOT / "assist-metrics",
            seed=9102,
            mode="qa",
            policy="hybrid",
        )
        decision = {
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "intent": "evade"},
        }
        # Two horizons. The controller totals are run-cumulative, so the second
        # observation's contribution is the delta, which is how planning-hold frames
        # (invisible in the per-horizon fields) get counted.
        for frames_total, deflection_total, per_horizon_max in ((180, 3600.0, 25.0), (300, 6600.0, 41.0)):
            recorder.record(
                step=0,
                decision=decision,
                observation={
                    "ok": True,
                    "controller": {
                        "assist_enabled": True,
                        "assist_frames_total": frames_total,
                        "assist_deflection_degrees_total": deflection_total,
                        "control_frames": 200,
                        "assist_max_deflection_degrees": per_horizon_max,
                    },
                },
                elapsed=0.0,
            )

        assist = recorder.assist_metrics()
        self.assertTrue(assist["enabled"])
        self.assertEqual(2, assist["horizons"])
        self.assertEqual(300, assist["assist_frames"])
        self.assertEqual(400, assist["control_frames"])
        self.assertEqual(22.0, assist["mean_deflection_degrees"])
        self.assertEqual(41.0, assist["max_deflection_degrees"])

    def test_recorder_reports_no_assist_for_the_llm_policy(self) -> None:
        recorder = RunRecorder(
            output_dir=TEST_TEMP_ROOT / "assist-metrics-llm",
            seed=9102,
            mode="qa",
            policy="llm",
        )
        recorder.record(
            step=0,
            decision={"tool": "game", "action": "direct_steer", "arguments": {"x": 1.0, "y": 0.0}},
            observation={"ok": True, "controller": {"assist_enabled": False}},
            elapsed=0.0,
        )

        self.assertEqual(
            {
                "enabled": False,
                "horizons": 0,
                "assist_frames": 0,
                "control_frames": 0,
                "assist_frame_ratio": 0.0,
                "mean_deflection_degrees": 0.0,
                "max_deflection_degrees": 0.0,
            },
            recorder.assist_metrics(),
        )

    def test_hybrid_manifest_reports_the_hybrid_prompt_version(self) -> None:
        base = RunRecorder(output_dir=TEST_TEMP_ROOT / "pv-llm", seed=1, mode="qa", policy="llm")
        hybrid = RunRecorder(output_dir=TEST_TEMP_ROOT / "pv-hybrid", seed=1, mode="qa", policy="hybrid")

        self.assertEqual("qa-planning/v7", base.effective_prompt_version())
        self.assertEqual("qa-planning/v7-hybrid-smart-v1", hybrid.effective_prompt_version())

    def test_hybrid_charter_declares_the_automatic_avoidance(self) -> None:
        control = TestCharter(bridge_assist=True).as_dict()["control_policy"]

        self.assertTrue(control["automatic_enemy_avoidance"])
        self.assertIn("0.6", control["automatic_enemy_avoidance_detail"])
        self.assertNotIn("only", control["bridge_role"])
        # The assist blends; it never re-aims or targets, so these stay true.
        self.assertFalse(control["automatic_chest_targeting"])
        self.assertFalse(control["automatic_direction_correction"])

    def test_hybrid_charter_prioritizes_item_use_without_changing_llm_priorities(self) -> None:
        hybrid_priority = TestCharter(bridge_assist=True).as_dict()["control_policy"][
            "priority_order"
        ]
        llm_priority = TestCharter().as_dict()["control_policy"]["priority_order"]

        self.assertEqual(
            [
                "survive",
                "urgent_item_use",
                "mission_progress",
                "collection",
                "qa_checks",
            ],
            hybrid_priority,
        )
        self.assertEqual(
            ["survive", "collect_reachable_chests", "net_heading_progress", "coverage"],
            llm_priority,
        )

    def test_hybrid_charter_publishes_the_assist_weight(self) -> None:
        navigation = TestCharter(bridge_assist=True, assist_survival_weight=0.9).as_dict()[
            "navigation_policy"
        ]

        self.assertEqual(0.9, navigation["assist_survival_weight"])
        self.assertNotIn("assist_survival_weight", TestCharter().as_dict()["navigation_policy"])

    def test_charter_rejects_an_out_of_range_assist_weight(self) -> None:
        with self.assertRaises(ValueError):
            TestCharter(assist_survival_weight=5.5)

    def test_hybrid_prompt_declares_the_assist(self) -> None:
        planner = LLMPlanner(
            "qa", "test-model", TestCharter(bridge_assist=True), 5.0, api_key="test-key"
        )
        prompt = planner._planning_system_prompt()

        for claim in NO_ASSIST_PROMPT_CLAIMS:
            self.assertNotIn(claim, prompt)
        self.assertIn("escape_vector * 0.6 * clamp01(0.35 + danger)", prompt)
        self.assertIn("controller.commanded", prompt)

    def test_llm_prompt_carries_the_selected_call_action_contract_guidance(self) -> None:
        """The contract now offers use_item under --policy llm, so the prompt must say so.

        A prompt that still claimed direct_steer only would make the agent treat its own
        allowed call as unavailable.
        """
        prompt = LLMPlanner(
            "qa", "test-model", TestCharter(), 5.0, api_key="test-key"
        )._planning_system_prompt()

        self.assertIn("argument_contracts contains the selected call", prompt)
        self.assertIn("also permits game.use_item", prompt)
        self.assertIn("Only choose use_item for a slot whose count is greater than zero.", prompt)
        for claim in NO_ASSIST_PROMPT_CLAIMS:
            self.assertIn(claim, prompt)

    def test_hybrid_prompt_uses_selected_call_action_contract_guidance(self) -> None:
        prompt = LLMPlanner(
            "qa", "test-model", TestCharter(bridge_assist=True), 5.0, api_key="test-key"
        )._planning_system_prompt()

        self.assertIn("argument_contracts contains the selected call", prompt)
        self.assertIn("also permits game.use_item", prompt)

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
