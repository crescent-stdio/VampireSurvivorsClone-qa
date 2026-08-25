from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qa_smoke.adapters import EpisodeExit, VampireSurvivorsAdapter
from qa_smoke.regression import DiffKind, ScenarioResult, diff_scenario_results
from qa_smoke.reporting import final_verdict
from qa_smoke.reporting import RunRecorder
from qa_smoke.reporting import build_run_verdict
from qa_smoke.evaluation import evaluate_v4_oracle
from qa_smoke import run as run_module
from qa_smoke import cli as cli_module
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

    def test_agent_observation_excludes_evaluator_state_and_projects_player_view(self) -> None:
        agent = build_agent_observation(
            {
                "player": {"health": 10.0},
                "evaluator_state": {"player": {"health": 10.0}},
                "player_view": {"health": 10.0},
                "agent_state": {"phase": "active_gameplay"},
            }
        )

        self.assertNotIn("evaluator_state", agent)
        self.assertNotIn("agent_state", agent)
        self.assertEqual({"health": 10.0}, agent["player_view"])

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
        self.assertEqual(14, len(scenarios))
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

    def test_v4_view_oracle_waits_for_raw_and_display_goal_evidence(self) -> None:
        player_only = [
            {
                "observation": {
                    "observation_id": "obs-player-only",
                    "player": {"present": True, "health": 5.0, "max_health": 10.0},
                }
            }
        ]
        reached = [
            *player_only,
            {
                "observation": {
                    "observation_id": "obs-player-and-view",
                    "player": {"present": True, "health": 5.0, "max_health": 10.0},
                    "player_view": {"health": 5.0, "health_ratio": 0.5},
                }
            },
        ]
        view_without_raw_health = [
            {
                "observation": {
                    "observation_id": "obs-view-without-raw-health",
                    "player": {"present": True},
                    "player_view": {"health": 5.0, "health_ratio": 0.5},
                }
            }
        ]

        self.assertEqual(
            "not_evaluated",
            evaluate_v4_oracle("view_state_match", player_only).verdict,
        )
        self.assertEqual(
            "not_evaluated",
            evaluate_v4_oracle(
                "view_state_match", view_without_raw_health
            ).verdict,
        )
        self.assertEqual("pass", evaluate_v4_oracle("view_state_match", reached).verdict)

    def test_v4_exp_oracle_waits_for_level_three_before_evaluating(self) -> None:
        level_two = [
            {
                "observation": {
                    "observation_id": "obs-level-two",
                    "player": {"present": True, "level": 2, "exp": 4.0},
                    "player_view": {"exp": 99.0},
                }
            }
        ]
        level_three = [
            {
                "observation": {
                    "observation_id": "obs-level-three",
                    "player": {"present": True, "level": 3, "exp": 4.0},
                    "player_view": {"exp": 99.0},
                }
            }
        ]

        self.assertEqual(
            "not_evaluated",
            evaluate_v4_oracle("exp_conservation", level_two).verdict,
        )
        self.assertEqual("fail", evaluate_v4_oracle("exp_conservation", level_three).verdict)

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

    def test_v4_gameplay_fault_oracles_detect_each_injected_contract_break(self) -> None:
        cases = {
            "upgrade_dialog_closes": [
                {
                    "observation": {
                        "observation_id": "obs-upgrade-before",
                        "phase": "upgrade_selection",
                        "menu": {"upgrade_open": True},
                    }
                },
                {
                    "decision": {"action": "select_upgrade"},
                    "observation": {
                        "observation_id": "obs-upgrade-after",
                        "phase": "upgrade_selection",
                        "menu": {"upgrade_open": True},
                    },
                },
            ],
            "movement_matches_input": [
                {
                    "observation": {
                        "observation_id": f"obs-movement-{index}",
                        "player": {"velocity": {"x": -1.0, "y": 0.0}},
                        "controller": {"steering": {"x": 1.0, "y": 0.0}},
                    }
                }
                for index in range(3)
            ],
            "regular_monster_spawning_continues": [
                {
                    "observation": {
                        "observation_id": "obs-spawn",
                        "progress": {"level_time": 23.0},
                        "evaluator_state": {
                            "telemetry": {
                                "regular_monsters_spawned": 20,
                                "regular_last_spawn_time": 18.0,
                                "regular_expected_spawn_delay": 0.5,
                                "regular_spawn_schedule_active": True,
                            }
                        },
                    }
                }
            ],
            "weapon_cooldown_repeats": [
                {
                    "observation": {
                        "observation_id": "obs-weapon",
                        "evaluator_state": {
                            "telemetry": {
                                "telemetry_time": 8.0,
                                "primary_weapon_attacks": 1,
                                "primary_weapon_first_attack_time": 1.0,
                                "primary_weapon_last_attack_time": 1.0,
                                "primary_weapon_expected_cooldown": 2.0,
                                "primary_weapon_max_interval_ratio": 0.0,
                            }
                        },
                    }
                }
            ],
            "contact_damage_respects_cooldown": [
                {
                    "observation": {
                        "observation_id": "obs-contact",
                        "evaluator_state": {
                            "telemetry": {
                                "contact_damage_hits": 3,
                                "contact_cooldown_resets": 1,
                                "contact_interval_samples": 2,
                                "contact_minimum_interval_ratio": 0.1,
                            }
                        },
                    }
                }
            ],
            "projectile_enemy_collision_applies": [
                {
                    "observation": {
                        "observation_id": "obs-projectile",
                        "evaluator_state": {
                            "telemetry": {
                                "projectile_enemy_collisions": 2,
                                "projectile_enemy_hits": 0,
                                "projectile_enemy_consumptions": 0,
                            }
                        },
                    }
                }
            ],
        }

        for oracle_id, transitions in cases.items():
            with self.subTest(oracle_id=oracle_id):
                self.assertEqual("fail", evaluate_v4_oracle(oracle_id, transitions).verdict)

    def test_generalized_gameplay_oracles_accept_contract_compliant_outcomes(self) -> None:
        cases = {
            "upgrade_dialog_closes": [
                {
                    "observation": {
                        "observation_id": "obs-upgrade-before",
                        "phase": "upgrade_selection",
                        "menu": {"upgrade_open": True},
                    }
                },
                {
                    "decision": {"action": "select_upgrade"},
                    "observation": {
                        "observation_id": "obs-upgrade-after",
                        "phase": "active_gameplay",
                        "menu": {"upgrade_open": False},
                    },
                },
            ],
            "movement_matches_input": [
                {
                    "observation": {
                        "observation_id": f"obs-movement-pass-{index}",
                        "player": {"velocity": {"x": 0.8, "y": 0.1}},
                        "controller": {"steering": {"x": 1.0, "y": 0.0}},
                    }
                }
                for index in range(3)
            ],
            "regular_monster_spawning_continues": [
                {
                    "observation": {
                        "observation_id": f"obs-spawn-pass-{index}",
                        "progress": {"level_time": 10.0 + index},
                        "evaluator_state": {
                            "telemetry": {
                                "regular_monsters_spawned": 10 + index,
                                "regular_last_spawn_time": 9.8 + index,
                                "regular_expected_spawn_delay": 0.5,
                                "regular_spawn_schedule_active": True,
                            }
                        },
                    }
                }
                for index in range(2)
            ],
            "weapon_cooldown_repeats": [
                {
                    "observation": {
                        "observation_id": "obs-weapon-pass",
                        "evaluator_state": {
                            "telemetry": {
                                "telemetry_time": 4.1,
                                "primary_weapon_attacks": 4,
                                "primary_weapon_first_attack_time": 1.0,
                                "primary_weapon_last_attack_time": 4.0,
                                "primary_weapon_expected_cooldown": 1.0,
                                "primary_weapon_max_interval_ratio": 1.02,
                            }
                        },
                    }
                }
            ],
            "contact_damage_respects_cooldown": [
                {
                    "observation": {
                        "observation_id": "obs-contact-pass",
                        "evaluator_state": {
                            "telemetry": {
                                "contact_damage_hits": 3,
                                "contact_cooldown_resets": 3,
                                "contact_interval_samples": 2,
                                "contact_minimum_interval_ratio": 0.98,
                            }
                        },
                    }
                }
            ],
            "projectile_enemy_collision_applies": [
                {
                    "observation": {
                        "observation_id": "obs-projectile-pass",
                        "evaluator_state": {
                            "telemetry": {
                                "projectile_enemy_collisions": 2,
                                "projectile_enemy_hits": 2,
                                "projectile_enemy_consumptions": 2,
                            }
                        },
                    }
                }
            ],
        }

        for oracle_id, transitions in cases.items():
            with self.subTest(oracle_id=oracle_id):
                self.assertEqual("pass", evaluate_v4_oracle(oracle_id, transitions).verdict)

    def test_v4_control_oracles_accept_their_valid_traces(self) -> None:
        valid_observation = [
            {
                "observation": {
                    "observation_id": "obs-valid",
                    "player": {
                        "present": True,
                        "health": 5.0,
                        "max_health": 10.0,
                        "health_ratio": 0.5,
                        "position": {"x": 0.0, "y": 0.0},
                    },
                    "world": {"qa_entities": []},
                }
            }
        ]
        normal_transitions = [
            {
                "decision": {"action": "observe"},
                "observation": {
                    "observation_id": "obs-before-upgrade",
                    "inventory": {"abilities": []},
                    "world": {"chest_count": 1},
                    "progress": {"coins_gained": 0, "damage_dealt": 0, "damage_taken": 0},
                },
            },
            {
                "decision": {"action": "select_upgrade"},
                "observation": {
                    "observation_id": "obs-after-upgrade",
                    "inventory": {"abilities": [{"id": "whip"}]},
                    "world": {"chest_count": 1},
                    "progress": {"coins_gained": 0, "damage_dealt": 0, "damage_taken": 0},
                },
            },
            {
                "decision": {"action": "observe"},
                "observation": {
                    "observation_id": "obs-chest",
                    "world": {"chest_count": 0},
                    "progress": {"coins_gained": 0, "damage_dealt": 0, "damage_taken": 0},
                    "event_state": {"type": "chest_collected"},
                },
            },
        ]
        stable_progression = [
            {
                "observation": {
                    "observation_id": "obs-long",
                    "player": {
                        "present": True,
                        "health": 10.0,
                        "max_health": 10.0,
                        "health_ratio": 1.0,
                        "exp": 1.0,
                        "next_level_exp": 2.0,
                        "exp_ratio": 0.5,
                        "level": 3,
                    },
                    "progress": {"level_time": 120.0},
                }
            }
        ]

        for oracle_id, transitions in (
            ("valid_observation", valid_observation),
            ("normal_state_transitions", normal_transitions),
            ("stable_long_progression", stable_progression),
        ):
            with self.subTest(oracle_id=oracle_id):
                self.assertEqual("pass", evaluate_v4_oracle(oracle_id, transitions).verdict)

    def test_v4_control_oracles_do_not_evaluate_without_required_evidence(self) -> None:
        no_player = [{"observation": {"observation_id": "obs-no-player"}}]
        no_normal_transitions = [{"observation": {"observation_id": "obs-no-transitions"}}]
        no_long_progression = [
            {
                "observation": {
                    "observation_id": "obs-short",
                    "player": {
                        "present": True,
                        "health": 10.0,
                        "max_health": 10.0,
                        "health_ratio": 1.0,
                        "exp": 1.0,
                        "next_level_exp": 2.0,
                        "exp_ratio": 0.5,
                        "level": 1,
                    },
                    "progress": {"level_time": 10.0},
                }
            }
        ]

        for oracle_id, transitions in (
            ("valid_observation", no_player),
            ("normal_state_transitions", no_normal_transitions),
            ("stable_long_progression", no_long_progression),
        ):
            with self.subTest(oracle_id=oracle_id):
                self.assertEqual("not_evaluated", evaluate_v4_oracle(oracle_id, transitions).verdict)

    def test_v4_rewrite_preserves_a_control_not_reached_verdict(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "control-easy-observation")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "verdict.json").write_text(
                json.dumps({"execution_status": "completed", "final_verdict": "PASS"}),
                encoding="utf-8",
            )
            (run_dir / "steps.jsonl").write_text(
                json.dumps({"observation": {"observation_id": "obs-no-player"}}) + "\n",
                encoding="utf-8",
            )

            cli_module._rewrite_v4_verdict(run_dir, scenario)

            rewritten = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual("not_evaluated", rewritten["oracle_verdict"])
            self.assertEqual("NOT_REACHED", rewritten["final_verdict"])

    def test_v4_rewrite_marks_an_unreadable_trace_as_error(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "control-easy-observation")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "verdict.json").write_text(
                json.dumps(
                    {
                        "execution_status": "completed",
                        "oracle_verdict": "pass",
                        "final_verdict": "PASS",
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "steps.jsonl").write_text("{not-json}\n", encoding="utf-8")

            cli_module._rewrite_v4_verdict(run_dir, scenario)

            rewritten = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual("qa-run-verdict/v2", rewritten["schema_version"])
            self.assertEqual("control-easy-observation", rewritten["v4_scenario_id"])
            self.assertEqual("valid_observation", rewritten["v4_oracle_id"])
            self.assertEqual("ERROR", rewritten["final_verdict"])
            self.assertIn("invalid v4 artifact", rewritten["error"])

    def test_v4_rewrite_redacts_filesystem_exception_details(self) -> None:
        scenario = next(
            item
            for item in load_v4_scenarios()
            if item.id == "control-easy-observation"
        )
        with tempfile.TemporaryDirectory() as directory:
            secret = "fakeSecretCredential123"
            run_dir = Path(directory) / secret
            run_dir.mkdir()
            (run_dir / "verdict.json").write_text(
                json.dumps({"execution_status": "completed", "final_verdict": "PASS"}),
                encoding="utf-8",
            )

            cli_module._rewrite_v4_verdict(run_dir, scenario)

            rewritten = json.loads(
                (run_dir / "verdict.json").read_text(encoding="utf-8")
            )
            serialized = json.dumps(rewritten)
            self.assertEqual("FileNotFoundError", rewritten["error_type"])
            self.assertNotIn(secret, serialized)
            self.assertNotIn(directory, serialized)

    def test_v4_rewrite_marks_an_oracle_contract_failure_as_error(self) -> None:
        scenario = SimpleNamespace(
            id="invalid-v4-scenario",
            oracle=SimpleNamespace(id="missing-oracle"),
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "verdict.json").write_text(
                json.dumps({"execution_status": "completed", "final_verdict": "PASS"}),
                encoding="utf-8",
            )
            (run_dir / "steps.jsonl").write_text(
                json.dumps({"observation": {"observation_id": "obs-1"}}) + "\n",
                encoding="utf-8",
            )

            cli_module._rewrite_v4_verdict(run_dir, scenario)

            rewritten = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual("ERROR", rewritten["final_verdict"])
            self.assertEqual("not_evaluated", rewritten["oracle_verdict"])
            self.assertIn("v4 oracle contract failure", rewritten["error"])

    def test_v4_rewrite_marks_a_nested_non_object_observation_as_error(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "control-easy-observation")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "verdict.json").write_text(
                json.dumps({"execution_status": "completed", "final_verdict": "PASS"}),
                encoding="utf-8",
            )
            (run_dir / "steps.jsonl").write_text(
                json.dumps({"observation": []}) + "\n",
                encoding="utf-8",
            )

            cli_module._rewrite_v4_verdict(run_dir, scenario)

            rewritten = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual("ERROR", rewritten["final_verdict"])
            self.assertIn("v4 oracle contract failure", rewritten["error"])

    def test_v4_suite_forwards_its_own_limits_to_the_run(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "medium-item-hit-range")
        captured: list[SimpleNamespace] = []
        args = SimpleNamespace(
            build=Path("player.app"),
            project_root=Path.cwd(),
            seed=None,
            headless=False,
            quiet=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(cli_module, "load_v4_scenarios", return_value=[scenario]),
                patch.object(cli_module, "load_v4_ground_truth", return_value={}),
                patch.object(cli_module, "run_session", side_effect=lambda run_args: captured.append(run_args) or 0),
            ):
                cli_module._run_v4_suite(args, Path(directory), injected=False)

        self.assertEqual(1, len(captured))
        self.assertEqual(120.0, captured[0].max_simulation_seconds)
        self.assertEqual(40, captured[0].max_steps)

    def test_v4_suite_accepts_a_configured_seed(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "medium-item-hit-range")
        captured: list[SimpleNamespace] = []
        args = SimpleNamespace(
            build=Path("player.app"),
            project_root=Path.cwd(),
            seed=9102,
            headless=False,
            quiet=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(cli_module, "load_v4_scenarios", return_value=[scenario]),
                patch.object(cli_module, "load_v4_ground_truth", return_value={}),
                patch.object(cli_module, "run_session", side_effect=lambda run_args: captured.append(run_args) or 0),
            ):
                cli_module._run_v4_suite(args, Path(directory), injected=False)

        self.assertEqual(9102, captured[0].seed)


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

    def test_bridge_scenario_id_can_be_overridden_for_v4_fault_scope(self) -> None:
        args = run_module.parse_args(
            [
                "--game-exe", "player.app", "--output", "artifacts",
                "--scenario", "easy-health-ratio",
                "--bridge-scenario-id", "easy-hp-on-hit",
            ]
        )

        self.assertEqual("easy-hp-on-hit", args.bridge_scenario_id)

    def test_cli_exposes_run_and_baseline_commands(self) -> None:
        run_args = parse_cli(["run", "--build", "player.app", "--suite", "v4-core"])
        baseline_args = parse_cli(["baseline", "set", "artifacts/run"])

        self.assertEqual("run", run_args.command)
        self.assertEqual("v4-core", run_args.suite)
        self.assertEqual("baseline", baseline_args.command)
        self.assertEqual("set", baseline_args.baseline_action)


class CliSuiteTests(unittest.TestCase):
    def test_verdict_reader_treats_array_and_scalar_payloads_as_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            for payload in ([], "not-an-object", 1):
                with self.subTest(payload=payload):
                    (artifact_dir / "verdict.json").write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual("ERROR", cli_module._verdict_from_artifact(artifact_dir))

    def test_v4_clean_and_injected_manifests_remain_separate(self) -> None:
        scenario = next(item for item in load_v4_scenarios() if item.id == "easy-hp-on-hit")
        args = SimpleNamespace(
            build=Path("player.app"),
            project_root=Path.cwd(),
            seed=None,
            headless=False,
            quiet=True,
        )

        def write_artifacts(run_args):
            run_args.output.mkdir(parents=True)
            (run_args.output / "verdict.json").write_text(
                json.dumps({"execution_status": "completed"}), encoding="utf-8"
            )
            (run_args.output / "steps.jsonl").write_text(
                json.dumps({"observation": {"observation_id": "obs-1"}}) + "\n",
                encoding="utf-8",
            )
            return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(cli_module, "load_v4_scenarios", return_value=[scenario]),
                patch.object(
                    cli_module,
                    "load_v4_ground_truth",
                    return_value={scenario.id: {"fault_id": "health_ratio_out_of_range"}},
                ),
                patch.object(cli_module, "run_session", side_effect=write_artifacts),
            ):
                clean_root = cli_module._run_v4_suite(args, root, injected=False, variant="clean")
                injected_root = cli_module._run_v4_suite(args, root, injected=True, variant="injected")

            clean_manifest = json.loads((clean_root / "suite-manifest.json").read_text(encoding="utf-8"))
            injected_manifest = json.loads((injected_root / "suite-manifest.json").read_text(encoding="utf-8"))

        self.assertFalse(clean_manifest["injected"])
        self.assertIsNone(clean_manifest["scenarios"][scenario.id]["fault_id"])
        self.assertTrue(injected_manifest["injected"])
        self.assertEqual(
            "health_ratio_out_of_range",
            injected_manifest["scenarios"][scenario.id]["fault_id"],
        )

    def test_legacy_injected_suite_forwards_faults_and_records_an_injected_manifest(self) -> None:
        scenario = next(item for item in load_scenarios() if item.id == "easy-health-ratio")
        args = SimpleNamespace(
            build=Path("player.app"),
            project_root=Path.cwd(),
            seed=None,
            headless=False,
            quiet=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "legacy-contract" / "injected" / scenario.id / "9101"
            result = SimpleNamespace(
                scenario_id=scenario.id,
                seed=9101,
                return_code=0,
                output_dir=output_dir,
            )
            with (
                patch.object(cli_module, "load_scenarios", return_value=[scenario]),
                patch.object(cli_module, "run_benchmark", return_value=[result]) as run_benchmark,
            ):
                suite_root = cli_module._run_legacy_suite(
                    args,
                    root,
                    injected=True,
                    variant="injected",
                )

            manifest = json.loads((suite_root / "suite-manifest.json").read_text(encoding="utf-8"))

        self.assertTrue(run_benchmark.call_args.kwargs["inject_faults"])
        self.assertTrue(manifest["injected"])
        self.assertEqual("health_ratio_out_of_range", manifest["scenarios"][scenario.id]["fault_id"])

    def test_validate_faults_all_runs_clean_and_injected_variants_for_both_suites(self) -> None:
        calls: list[tuple[str, bool, str | None]] = []

        def write_suite(suite: str):
            def run_suite(_args, root, *, injected, variant=None):
                calls.append((suite, injected, variant))
                suite_root = root / suite / str(variant)
                output_dir = suite_root / "probe"
                output_dir.mkdir(parents=True)
                (output_dir / "verdict.json").write_text(
                    json.dumps({"final_verdict": "FAIL" if injected else "PASS"}),
                    encoding="utf-8",
                )
                (suite_root / "suite-manifest.json").write_text(
                    json.dumps(
                        {
                            "suite": suite,
                            "variant": variant,
                            "scenarios": {
                                "probe": {
                                    "output_dir": str(output_dir),
                                    "fault_id": "injected-probe" if injected else None,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return suite_root

            return run_suite

        with tempfile.TemporaryDirectory() as directory:
            args = parse_cli(
                [
                    "validate-faults",
                    "--build",
                    "player.app",
                    "--suite",
                    "all",
                    "--output",
                    directory,
                ]
            )
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=write_suite("v4-core")),
                patch.object(cli_module, "_run_legacy_suite", side_effect=write_suite("legacy-contract")),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    return_value={
                        "v4-core": {"probe": "injected-probe"},
                        "legacy-contract": {"probe": "injected-probe"},
                    },
                ),
            ):
                exit_code = cli_module._run_command(args)

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [
                ("v4-core", False, "clean"),
                ("v4-core", True, "injected"),
                ("legacy-contract", False, "clean"),
                ("legacy-contract", True, "injected"),
            ],
            calls,
        )

    def test_validate_faults_keeps_clean_results_in_json_report_and_exit_status(self) -> None:
        def run_v4(_args, root, *, injected, variant=None):
            suite_root = root / "v4-core" / str(variant)
            artifact_dir = suite_root / "probe"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verdict.json").write_text(
                json.dumps({"final_verdict": "PASS" if injected else "ERROR"}),
                encoding="utf-8",
            )
            (suite_root / "suite-manifest.json").write_text(
                json.dumps(
                    {
                        "suite": "v4-core",
                        "variant": variant,
                        "scenarios": {
                            "probe": {
                                "output_dir": str(artifact_dir),
                                "fault_id": None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            return suite_root

        with tempfile.TemporaryDirectory() as directory:
            args = parse_cli(
                ["validate-faults", "--build", "player.app", "--output", directory]
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    return_value={"v4-core": {"probe": None}},
                ),
                redirect_stdout(output),
            ):
                exit_code = cli_module._run_command(args)

            payload = json.loads(output.getvalue())
            report = (Path(directory) / "report.md").read_text(encoding="utf-8")

        self.assertEqual(2, exit_code)
        self.assertEqual(
            {
                "v4-core/clean/probe": "ERROR",
                "v4-core/injected/probe": "PASS",
            },
            payload["results"],
        )
        self.assertIn("v4-core/clean/probe", report)
        self.assertIn("v4-core/injected/probe", report)

    def test_validate_faults_rejects_missing_injected_expectation_contract(self) -> None:
        def run_v4(_args, root, *, injected, variant=None):
            suite_root = root / "v4-core" / str(variant)
            artifact_dir = suite_root / "probe"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "verdict.json").write_text(
                json.dumps({"final_verdict": "PASS"}),
                encoding="utf-8",
            )
            scenario = {"output_dir": str(artifact_dir)}
            if not injected:
                scenario["fault_id"] = None
            (suite_root / "suite-manifest.json").write_text(
                json.dumps(
                    {
                        "suite": "v4-core",
                        "variant": variant,
                        "scenarios": {"probe": scenario},
                    }
                ),
                encoding="utf-8",
            )
            return suite_root

        with tempfile.TemporaryDirectory() as directory:
            args = parse_cli(
                ["validate-faults", "--build", "player.app", "--output", directory]
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    return_value={"v4-core": {"probe": None}},
                ),
                redirect_stdout(output),
            ):
                exit_code = cli_module._run_command(args)
            report = (Path(directory) / "report.md").read_text(encoding="utf-8")

        self.assertEqual(2, exit_code)
        self.assertEqual(
            "CampaignContractError",
            json.loads(output.getvalue())["error_type"],
        )
        self.assertNotIn("fault_id", report)
        self.assertNotIn(directory, report)

    def test_validate_faults_requires_nonempty_exact_clean_injected_pairs(self) -> None:
        for malformed_pair in ("empty-injected", "duplicate-clean", "mismatched-set"):
            with self.subTest(malformed_pair=malformed_pair):
                def run_v4(_args, root, *, injected, variant=None):
                    manifest_variant = (
                        "clean" if malformed_pair == "duplicate-clean" else variant
                    )
                    scenario_id = (
                        "fault-probe"
                        if injected and malformed_pair == "mismatched-set"
                        else "probe"
                    )
                    scenarios = {}
                    suite_root = root / "v4-core" / str(variant)
                    suite_root.mkdir(parents=True, exist_ok=True)
                    if not (injected and malformed_pair == "empty-injected"):
                        artifact_dir = suite_root / scenario_id
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        (artifact_dir / "verdict.json").write_text(
                            json.dumps(
                                {"final_verdict": "FAIL" if injected else "PASS"}
                            ),
                            encoding="utf-8",
                        )
                        scenarios[scenario_id] = {
                            "output_dir": str(artifact_dir),
                            "fault_id": "injected-probe" if injected else None,
                        }
                    (suite_root / "suite-manifest.json").write_text(
                        json.dumps(
                            {
                                "suite": "v4-core",
                                "variant": manifest_variant,
                                "scenarios": scenarios,
                            }
                        ),
                        encoding="utf-8",
                    )
                    return suite_root

                with tempfile.TemporaryDirectory() as directory:
                    args = parse_cli(
                        [
                            "validate-faults",
                            "--build",
                            "player.app",
                            "--output",
                            directory,
                        ]
                    )
                    output = StringIO()
                    with (
                        patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                        patch.object(
                            cli_module,
                            "_authoritative_fault_bindings",
                            return_value={
                                "v4-core": {"probe": "injected-probe"}
                            },
                        ),
                        redirect_stdout(output),
                    ):
                        exit_code = cli_module._run_command(args)

                self.assertEqual(2, exit_code)
                self.assertEqual(
                    "CampaignContractError",
                    json.loads(output.getvalue())["error_type"],
                )

    def test_validate_faults_rejects_manifest_drift_from_authoritative_registry(self) -> None:
        for drift in ("omitted-fault", "tampered-fault-id"):
            with self.subTest(drift=drift):
                authoritative = {"probe": "injected-probe"}
                if drift == "omitted-fault":
                    authoritative["required-fault"] = "required-fault-id"

                def run_v4(_args, root, *, injected, variant=None):
                    suite_root = root / "v4-core" / str(variant)
                    artifact_dir = suite_root / "probe"
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    injected_fault_id = (
                        None
                        if not injected or drift == "tampered-fault-id"
                        else "injected-probe"
                    )
                    verdict = "FAIL" if injected_fault_id else "PASS"
                    (artifact_dir / "verdict.json").write_text(
                        json.dumps({"final_verdict": verdict}),
                        encoding="utf-8",
                    )
                    (suite_root / "suite-manifest.json").write_text(
                        json.dumps(
                            {
                                "suite": "v4-core",
                                "variant": variant,
                                "scenarios": {
                                    "probe": {
                                        "output_dir": str(artifact_dir),
                                        "fault_id": injected_fault_id if injected else None,
                                    }
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    return suite_root

                with tempfile.TemporaryDirectory() as directory:
                    args = parse_cli(
                        [
                            "validate-faults",
                            "--build",
                            "player.app",
                            "--output",
                            directory,
                        ]
                    )
                    output = StringIO()
                    with (
                        patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                        patch.object(
                            cli_module,
                            "_authoritative_fault_bindings",
                            create=True,
                            return_value={"v4-core": authoritative},
                        ),
                        redirect_stdout(output),
                    ):
                        exit_code = cli_module._run_command(args)

                self.assertEqual(2, exit_code)
                self.assertEqual(
                    "CampaignContractError",
                    json.loads(output.getvalue())["error_type"],
                )

    def test_validate_faults_sanitizes_invalid_artifact_binding(self) -> None:
        def run_v4(_args, root, *, injected, variant=None):
            suite_root = root / "v4-core" / str(variant)
            suite_root.mkdir(parents=True, exist_ok=True)
            output_dir = "\0" if injected else str(suite_root / "probe")
            if not injected:
                artifact_dir = Path(output_dir)
                artifact_dir.mkdir()
                (artifact_dir / "verdict.json").write_text(
                    json.dumps({"final_verdict": "PASS"}),
                    encoding="utf-8",
                )
            (suite_root / "suite-manifest.json").write_text(
                json.dumps(
                    {
                        "suite": "v4-core",
                        "variant": variant,
                        "scenarios": {
                            "probe": {
                                "output_dir": output_dir,
                                "fault_id": "injected-probe" if injected else None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            return suite_root

        with tempfile.TemporaryDirectory() as directory:
            args = parse_cli(
                ["validate-faults", "--build", "player.app", "--output", directory]
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    create=True,
                    return_value={"v4-core": {"probe": "injected-probe"}},
                ),
                redirect_stdout(output),
            ):
                try:
                    exit_code = cli_module._run_command(args)
                except (OSError, UnicodeError, ValueError) as error:
                    self.fail(f"artifact contract exception escaped: {type(error).__name__}")
            report = (Path(directory) / "report.md").read_text(encoding="utf-8")

        self.assertEqual(2, exit_code)
        self.assertEqual(
            "CampaignContractError",
            json.loads(output.getvalue())["error_type"],
        )
        self.assertNotIn("\\u0000", report)

    def test_approved_clean_run_baseline_is_consumed_by_validate_faults(self) -> None:
        def run_v4(_args, root, *, injected, variant=None):
            if variant is None:
                suite_root = root / "v4-core"
            else:
                suite_root = root / "v4-core" / str(variant)
            artifact_dir = suite_root / "probe"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "verdict.json").write_text(
                json.dumps(
                    {"final_verdict": "FAIL" if variant == "injected" else "PASS"}
                ),
                encoding="utf-8",
            )
            (suite_root / "suite-manifest.json").write_text(
                json.dumps(
                    {
                        "suite": "v4-core",
                        "variant": variant,
                        "scenarios": {
                            "probe": {
                                "output_dir": str(artifact_dir),
                                "fault_id": (
                                    "injected-probe" if variant == "injected" else None
                                ),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            return suite_root

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved_run = root / "approved-run"
            baseline = root / "baseline.json"
            validate_output = root / "validate-output"
            run_args = parse_cli(
                [
                    "run",
                    "--build",
                    "player.app",
                    "--output",
                    str(approved_run),
                ]
            )
            baseline_args = parse_cli(
                [
                    "baseline",
                    "set",
                    str(approved_run),
                    "--path",
                    str(baseline),
                ]
            )
            validate_args = parse_cli(
                [
                    "validate-faults",
                    "--build",
                    "player.app",
                    "--output",
                    str(validate_output),
                    "--baseline",
                    str(baseline),
                ]
            )
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    return_value={"v4-core": {"probe": "injected-probe"}},
                ),
            ):
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, cli_module._run_command(run_args))
                    self.assertEqual(0, cli_module._baseline_set(baseline_args))
                    exit_code = cli_module._run_command(validate_args)

            baseline_payload = json.loads(baseline.read_text(encoding="utf-8"))
            diff = json.loads(
                (validate_output / "regression-diff.json").read_text(encoding="utf-8")
            )
            report = (validate_output / "report.md").read_text(encoding="utf-8")

        self.assertEqual({"probe"}, set(baseline_payload["scenarios"]))
        self.assertEqual(0, exit_code)
        self.assertEqual("qa-regression-diff/v2", diff["schema_version"])
        self.assertEqual("paired-clean-only", diff["baseline_scope"])
        self.assertEqual(
            {"v4-core/clean/probe"},
            set(diff["clean_baseline_diffs"]),
        )
        self.assertEqual(
            {
                "v4-core/injected/probe": {
                    "verdict": "FAIL",
                    "stability": "stable",
                    "expected_verdict": "FAIL",
                    "matches_expected": True,
                }
            },
            diff["injected_current_results"],
        )
        self.assertIn("paired clean variants only", report)
        self.assertIn("Injected current oracle results", report)

    def test_validate_faults_baseline_rejects_clean_not_reached(self) -> None:
        def run_v4(_args, root, *, injected, variant=None):
            suite_root = root / "v4-core" / str(variant)
            artifact_dir = suite_root / "probe"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            verdict = "FAIL" if injected else "NOT_REACHED"
            (artifact_dir / "verdict.json").write_text(
                json.dumps({"final_verdict": verdict}),
                encoding="utf-8",
            )
            (suite_root / "suite-manifest.json").write_text(
                json.dumps(
                    {
                        "suite": "v4-core",
                        "variant": variant,
                        "scenarios": {
                            "probe": {
                                "output_dir": str(artifact_dir),
                                "fault_id": "injected-probe" if injected else None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            return suite_root

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": "qa-regression-baseline/v1",
                        "scenarios": {
                            "probe": {"verdict": "PASS", "stability": "stable"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = parse_cli(
                [
                    "validate-faults",
                    "--build",
                    "player.app",
                    "--output",
                    str(root / "validate-output"),
                    "--baseline",
                    str(baseline),
                ]
            )
            with (
                patch.object(cli_module, "_run_v4_suite", side_effect=run_v4),
                patch.object(
                    cli_module,
                    "_authoritative_fault_bindings",
                    return_value={"v4-core": {"probe": "injected-probe"}},
                ),
                redirect_stdout(StringIO()),
            ):
                exit_code = cli_module._run_command(args)
            diff = json.loads(
                (root / "validate-output" / "regression-diff.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(1, exit_code)
        self.assertEqual(
            "NEWLY NOT_REACHED",
            diff["clean_baseline_diffs"]["v4-core/clean/probe"]["kind"],
        )


if __name__ == "__main__":
    unittest.main()
