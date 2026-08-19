from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qa_smoke.adapters import EpisodeExit, VampireSurvivorsAdapter
from qa_smoke.regression import DiffKind, ScenarioResult, diff_scenario_results
from qa_smoke.reporting import final_verdict
from qa_smoke.reporting import RunRecorder
from qa_smoke.reporting import build_run_verdict
from qa_smoke.evaluation import evaluate_v4_oracle
from qa_smoke import run as run_module
from qa_smoke.cli import parse_cli
from qa_smoke.scenarios import load_scenarios, load_v4_scenarios
from qa_smoke.state_channels import build_state_channels, build_agent_observation


class FakeBridge:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.process = SimpleNamespace(returncode=None)
        self.closed = False
        self.commands: list[tuple[str, dict]] = []

    def launch(self):
        return {"ready": True}

    def command(self, action: str, **parameters):
        self.commands.append((action, parameters))
        return {
            "protocol_version": "1.5",
            "observation_id": f"obs-{len(self.commands)}",
            "action": action,
            "player": {"health_ratio": 1.25},
        }

    def close(self):
        self.closed = True


class AdapterContractTests(unittest.TestCase):
    def test_adapter_passes_explicit_faults_and_returns_raw_observation(self) -> None:
        created: list[FakeBridge] = []

        def factory(**kwargs):
            bridge = FakeBridge(**kwargs)
            created.append(bridge)
            return bridge

        adapter = VampireSurvivorsAdapter(
            game_exe=Path("/tmp/game"),
            session_dir=Path("/tmp/session"),
            mode="qa",
            bridge_factory=factory,
        )

        adapter.start(seed=9101, preset="smoke", faults=["health_ratio_out_of_range"])
        adapter.send_input({"action": "observe", "decision_id": "d1"})

        self.assertEqual("health_ratio_out_of_range", created[0].kwargs["fault_id"])
        self.assertEqual(1.25, adapter.read_state()["player"]["health_ratio"])

    def test_adapter_rejects_multiple_faults_and_maps_close_to_normal_exit(self) -> None:
        adapter = VampireSurvivorsAdapter(
            game_exe=Path("/tmp/game"),
            session_dir=Path("/tmp/session"),
            mode="qa",
            bridge_factory=FakeBridge,
        )
        with self.assertRaises(ValueError):
            adapter.start(seed=1, preset="smoke", faults=["a", "b"])

        adapter.start(seed=1, preset="smoke", faults=[])
        self.assertEqual(EpisodeExit(kind="normal"), adapter.stop())


class StateChannelTests(unittest.TestCase):
    def test_channels_preserve_raw_state_and_hide_advisory_from_agent(self) -> None:
        observation = {
            "player": {"health_ratio": 1.25},
            "world": {"danger_score": 0.9, "escape_vector": {"x": -1, "y": 0}},
            "fault_id": "secret",
            "ground_truth": {"bug_id": "secret"},
            "available_actions": ["observe"],
        }

        channels = build_state_channels(observation)
        agent = build_agent_observation(observation)

        self.assertEqual(1.25, channels["evaluator_state"]["player"]["health_ratio"])
        self.assertEqual(0.9, channels["harness_advisory"]["danger_score"])
        self.assertNotIn("fault_id", agent)
        self.assertNotIn("ground_truth", agent)
        self.assertNotIn("danger_score", json.dumps(agent))

    def test_agent_observation_does_not_include_evaluator_or_view_channels(self) -> None:
        agent = build_agent_observation(
            {
                "player": {"health": 10.0},
                "evaluator_state": {"player": {"health": 10.0}},
                "player_view": {"health": 10.0},
                "agent_state": {"phase": "active_gameplay"},
            }
        )

        self.assertNotIn("evaluator_state", agent)
        self.assertNotIn("player_view", agent)
        self.assertNotIn("agent_state", agent)

    def test_view_state_mismatch_is_recorded_as_always_on_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "qa", "heuristic", 1)
            observation = {
                "observation_id": "obs-1",
                "ok": True,
                "player": {"present": True, "health": 5.0, "max_health": 10.0, "health_ratio": 0.5},
                "player_view": {"health_ratio": 1.0},
                "progress": {"level_time": 1.0},
                "world": {},
                "menu": {},
                "event_state": {},
            }
            recorder.record(0, {"tool": "game", "action": "observe"}, observation, 0.0)

            self.assertTrue(any(item["kind"] == "view_state_match" for item in recorder.anomalies))

    def test_repeated_active_frame_is_recorded_as_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "qa", "heuristic", 1)
            for step in range(2):
                recorder.record(
                    step,
                    {"tool": "game", "action": "observe"},
                    {
                        "observation_id": f"obs-{step}",
                        "ok": True,
                        "frame": 10,
                        "paused": False,
                        "phase": "active_gameplay",
                        "player": {"present": True, "health": 10.0, "max_health": 10.0},
                        "progress": {"level_time": 1.0},
                        "world": {},
                        "menu": {},
                        "event_state": {},
                    },
                    0.0,
                )

            self.assertTrue(any(item["kind"] == "freeze" for item in recorder.anomalies))


class V4ScenarioTests(unittest.TestCase):
    def test_v4_suite_contains_three_bug_types_and_verified_paths(self) -> None:
        scenarios = load_v4_scenarios()
        self.assertEqual(8, len(scenarios))
        self.assertEqual(
            {"logic_error", "description_flaw", "data_inconsistency", "control"},
            {scenario.bug_type for scenario in scenarios},
        )
        for scenario in scenarios:
            self.assertTrue(scenario.goal.verified_path.startswith("scripted:seed="))
            self.assertIn(scenario.goal.verified_seed, scenario.seeds)

    def test_legacy_scenarios_remain_loadable(self) -> None:
        self.assertEqual(9, len(load_scenarios()))

    def test_v4_view_oracle_detects_raw_and_display_mismatch(self) -> None:
        transitions = [
            {
                "observation": {
                    "observation_id": "obs-1",
                    "player": {"present": True, "health": 5.0, "max_health": 10.0},
                    "player_view": {"health": 10.0, "max_health": 10.0},
                }
            }
        ]
        result = evaluate_v4_oracle("view_state_match", transitions)
        self.assertEqual("fail", result.verdict)

    def test_v4_item_range_oracle_compares_affected_targets(self) -> None:
        transitions = [
            {
                "observation": {
                    "observation_id": "obs-item-1",
                    "event_state": {
                        "event_id": "event-item-1",
                        "type": "item_used",
                        "targets_affected": 1,
                        "targets_in_radius": 3,
                    },
                }
            }
        ]

        result = evaluate_v4_oracle("item_hit_range", transitions)

        self.assertEqual("fail", result.verdict)


class VerdictAndDiffTests(unittest.TestCase):
    def test_final_verdict_applies_error_fail_not_reached_pass_precedence(self) -> None:
        self.assertEqual(
            "ERROR",
            final_verdict("infrastructure_error", "reached", "pass", []),
        )
        self.assertEqual(
            "FAIL",
            final_verdict("completed", "reached", "fail", []),
        )
        self.assertEqual(
            "NOT_REACHED",
            final_verdict("completed", "not_reached", "not_evaluated", []),
        )
        self.assertEqual(
            "PASS",
            final_verdict("completed", "reached", "pass", []),
        )
        self.assertEqual(
            "FAIL",
            final_verdict(
                "completed", "reached", "pass", [{"kind": "view_state_match"}, {"kind": "freeze"}]
            ),
        )
        self.assertEqual(
            "FAIL",
            build_run_verdict(
                execution_status="completed",
                coverage_status="reached",
                oracle_verdict="pass",
                agent_detection="not_evaluated",
                evidence_refs=["obs-1"],
                anomalies=[{"kind": "view_state_match"}],
            )["final_verdict"],
        )

    def test_baseline_diff_classifies_new_fail_and_fixed(self) -> None:
        baseline = {
            "easy-hp-on-hit": ScenarioResult("PASS", "stable"),
            "medium-item-effect": ScenarioResult("FAIL", "stable"),
        }
        current = {
            "easy-hp-on-hit": ScenarioResult("FAIL", "stable"),
            "medium-item-effect": ScenarioResult("PASS", "stable"),
        }

        diff = diff_scenario_results(baseline, current)

        self.assertEqual(DiffKind.NEW_FAIL, diff["easy-hp-on-hit"].kind)
        self.assertEqual(DiffKind.FIXED, diff["medium-item-effect"].kind)

    def test_run_arguments_keep_fault_injection_explicit(self) -> None:
        clean = run_module.parse_args(
            ["--game-exe", "player.app", "--output", "artifacts", "--scenario", "easy-health-ratio"]
        )
        injected = run_module.parse_args(
            [
                "--game-exe", "player.app", "--output", "artifacts",
                "--scenario", "easy-health-ratio", "--fault", "health_ratio_out_of_range",
            ]
        )
        self.assertEqual("", clean.fault)
        self.assertEqual("health_ratio_out_of_range", injected.fault)

    def test_cli_exposes_run_and_baseline_commands(self) -> None:
        run_args = parse_cli(["run", "--build", "player.app", "--suite", "v4-core"])
        baseline_args = parse_cli(["baseline", "set", "artifacts/run"])

        self.assertEqual("run", run_args.command)
        self.assertEqual("v4-core", run_args.suite)
        self.assertEqual("baseline", baseline_args.command)
        self.assertEqual("set", baseline_args.baseline_action)


if __name__ == "__main__":
    unittest.main()
