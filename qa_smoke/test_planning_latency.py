from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from .charter import TestCharter
from .memory import PlanningHistory
from .planners import (
    LLMPlanner,
    build_reflection_contract,
    compact_inventory,
    compact_observed_delta,
    compact_planning_transition,
    inject_reflection_evidence_refs,
)
from .reporting import RunRecorder


class PlannerContextCompactionTests(unittest.TestCase):
    def test_compact_inventory_keeps_slots_and_owned_abilities_only(self) -> None:
        inventory = {
            "slots": [
                {
                    "index": 0,
                    "type": "Potion",
                    "count": 2,
                    "pending_count": 1,
                    "description": "discarded",
                }
            ],
            "abilities": [
                {"type": "Axe", "name": "Axe", "level": 2, "owned": True},
                {"type": "Garlic", "name": "Garlic", "level": 0, "owned": False},
            ],
            "debug": "discarded",
        }

        self.assertEqual(
            {
                "slots": [
                    {"index": 0, "type": "Potion", "count": 2, "pending_count": 1}
                ],
                "abilities": [{"name": "Axe", "level": 2}],
            },
            compact_inventory(inventory),
        )

    def test_compact_observed_delta_compacts_inventory_without_mutating_input(self) -> None:
        delta = {
            "before_observation_id": "obs-1",
            "after_observation_id": "obs-2",
            "changes": [
                {
                    "path": "inventory",
                    "before": {
                        "slots": [],
                        "abilities": [
                            {"name": "Axe", "level": 1, "owned": True, "debug": "drop"}
                        ],
                    },
                    "after": {
                        "slots": [],
                        "abilities": [
                            {"name": "Axe", "level": 2, "owned": True, "debug": "drop"}
                        ],
                    },
                },
                {"path": "player.health", "before": 100.0, "after": 75.0},
            ],
            "debug": "drop",
        }
        original = copy.deepcopy(delta)

        compacted = compact_observed_delta(delta)

        self.assertEqual(original, delta)
        self.assertEqual("obs-2", compacted["after_observation_id"])
        self.assertEqual(
            {
                "path": "inventory",
                "before": {"slots": [], "abilities": [{"name": "Axe", "level": 1}]},
                "after": {"slots": [], "abilities": [{"name": "Axe", "level": 2}]},
            },
            compacted["changes"][0],
        )
        self.assertEqual(
            {"path": "player.health", "before": 100.0, "after": 75.0},
            compacted["changes"][1],
        )

    def test_compact_planning_transition_has_latest_and_history_shapes(self) -> None:
        transition = {
            "step": 2,
            "game_action": {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {
                    "x": 1.0,
                    "y": 0.0,
                    "duration": 5.0,
                    "intent": "collect_chest",
                    "target_id": 7,
                },
                "expected_effect": "The player should approach chest 7.",
                "reflection": {
                    "status": "matched",
                    "summary": "The player advanced toward the chest.",
                    "evidence_refs": ["obs-2"],
                    "candidate_id": "",
                    "reproduction_attempted": False,
                },
                "evaluation_context": {"debug": "drop"},
                "observed_delta": {"debug": "drop"},
            },
            "observation_id": "obs-2",
            "inventory": {
                "slots": [],
                "abilities": [{"name": "Axe", "level": 2, "owned": True}],
            },
            "observed_delta": {
                "before_observation_id": "obs-1",
                "after_observation_id": "obs-2",
                "changes": [],
            },
            "navigation": {"danger_score": 0.1, "target_id": 7},
        }

        latest = compact_planning_transition(transition, include_latest_details=True)
        history = compact_planning_transition(transition, include_latest_details=False)

        self.assertIn("expected_effect", latest)
        self.assertIn("reflection", latest)
        self.assertIn("inventory", latest)
        self.assertNotIn("evaluation_context", json.dumps(latest))
        self.assertNotIn("observed_delta", json.dumps(latest["action"]))
        self.assertEqual(
            {"tool": "game", "action": "direct_steer", "intent": "collect_chest", "target_id": 7},
            history["action"],
        )
        self.assertNotIn("expected_effect", history)
        self.assertNotIn("reflection", history)

    def test_planning_payload_has_one_compact_prior_outcome(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        transition = {
            "observation_id": "obs-2",
            "game_action": {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"intent": "evade", "target_id": 0},
                "expected_effect": "The player should avoid the threat.",
                "reflection": {
                    "status": "matched",
                    "summary": "The threat was avoided.",
                    "evidence_refs": ["obs-2"],
                    "candidate_id": "",
                    "reproduction_attempted": False,
                },
                "evaluation_context": {"debug": "drop"},
            },
            "inventory": {"slots": [], "abilities": []},
            "observed_delta": {
                "before_observation_id": "obs-1",
                "after_observation_id": "obs-2",
                "changes": [],
            },
            "navigation": {"danger_score": 0.8},
        }

        payload = planner._planning_payload(
            {
                "available_actions": ["direct_steer"],
                "inventory": {
                    "slots": [],
                    "abilities": [{"name": "Axe", "level": 1, "owned": True}],
                },
            },
            3,
            [transition],
            0,
        )

        self.assertEqual("obs-2", payload["prior_outcome"]["observation_id"])
        self.assertNotIn("previous_transition", payload)
        self.assertNotIn("recent_transitions", payload)
        self.assertNotIn("observed_delta", payload)
        self.assertNotIn("session_memory", payload)
        self.assertNotIn("test_charter", payload)
        self.assertNotIn("evaluation_context", json.dumps(payload))
        self.assertEqual(
            {"slots": [], "abilities": [{"name": "Axe", "level": 1}]},
            payload["observation"]["inventory"],
        )


class ReflectionEvidenceTests(unittest.TestCase):
    def test_source_tool_transition_uses_delta_after_id_as_evidence_fallback(self) -> None:
        contract = build_reflection_contract(
            {
                "decision": {"tool": "source_read"},
                "observed_delta": {
                    "before_observation_id": "",
                    "after_observation_id": "obs-source-after",
                    "changes": [],
                },
            }
        )

        self.assertEqual(["obs-source-after"], contract["allowed_evidence_refs"])

    def test_runner_injection_replaces_stale_model_evidence_with_latest_ids(self) -> None:
        decision = {
            "tool": "game",
            "action": "direct_steer",
            "arguments": {"x": 1.0, "y": 0.0, "duration": 2.0},
            "reflection": {
                "status": "matched",
                "summary": "The outcome matched.",
                "evidence_refs": ["obs-00000005"],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }
        contract = {
            "has_previous_transition": True,
            "allowed_evidence_refs": ["obs-00000007", "event-00000007"],
        }

        injected = inject_reflection_evidence_refs(decision, contract)

        self.assertEqual(
            ["obs-00000007", "event-00000007"],
            injected["reflection"]["evidence_refs"],
        )
        self.assertEqual(["obs-00000005"], decision["reflection"]["evidence_refs"])


class PlanningHistoryTests(unittest.TestCase):
    @staticmethod
    def transition(step: int) -> dict[str, object]:
        return {
            "step": step,
            "observation_id": f"obs-{step}",
            "player": {"position": {"x": float(step), "y": 0.0}, "level": 1},
            "progress": {"level_time": float(step)},
            "event_state": {},
        }

    def test_history_rolls_over_at_25_and_retains_latest_12_exchanges(self) -> None:
        history = PlanningHistory()

        for step in range(25):
            history.commit(f"user-{step}", f"assistant-{step}", [self.transition(step)])

        messages = history.messages()

        self.assertEqual(24, len(messages))
        self.assertEqual("user-13", messages[0]["content"])
        self.assertEqual("assistant-24", messages[-1]["content"])
        self.assertEqual(13, history.checkpoint_summary()["compressed_transition_count"])

    def test_history_rolls_over_again_every_12_accepted_exchanges(self) -> None:
        history = PlanningHistory()

        for step in range(37):
            history.commit(f"user-{step}", f"assistant-{step}", [self.transition(step)])

        messages = history.messages()

        self.assertEqual(24, len(messages))
        self.assertEqual("user-25", messages[0]["content"])
        self.assertEqual("assistant-36", messages[-1]["content"])
        self.assertEqual(25, history.checkpoint_summary()["compressed_transition_count"])

    def test_llm_planner_does_not_commit_pending_plan_until_explicit_commit(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        response = {
            "plan": "Observe.",
            "hypothesis": "",
            "qa_observation": "",
            "tool": "game",
            "action": "observe",
            "arguments": {},
            "expected_effect": "The observation should refresh.",
            "reflection": {
                "status": "not_applicable",
                "summary": "No previous transition exists.",
                "evidence_refs": [],
                "candidate_id": "",
                "reproduction_attempted": False,
            },
        }

        with patch.object(planner, "_request", return_value=response):
            planner.plan({"available_actions": ["observe"]}, 0, [])

        self.assertEqual([], planner._planning_history.messages())

        planner.commit_plan(response)

        self.assertEqual(2, len(planner._planning_history.messages()))

    def test_repaired_plan_commits_without_invalid_assistant_message(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        invalid = {
            "plan": "Invalid.",
            "hypothesis": "",
            "qa_observation": "",
            "tool": "game",
            "action": "observe",
            "arguments": {},
        }
        repaired = {**invalid, "plan": "Repaired."}
        with patch.object(planner, "_request", side_effect=[invalid, repaired]):
            planner.plan({"available_actions": ["observe"]}, 0, [])
            planner.repair_plan(
                {"available_actions": ["observe"]},
                0,
                [],
                invalid,
                "contract error",
            )

        planner.commit_plan(repaired)
        contents = [message["content"] for message in planner._planning_history.messages()]

        self.assertIn("Repaired.", contents[-1])
        self.assertNotIn("Invalid.", contents)

    def test_final_assessment_does_not_include_planning_history(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        planner._planning_history.commit("old-user", "old-assistant", [])
        with patch.object(planner, "_request", return_value={}) as request:
            planner.final_assessment({"confirmed_hypotheses": []})

        self.assertFalse(request.call_args.kwargs.get("include_planning_history", True))
        self.assertEqual(2, len(planner._planning_history.messages()))

    def test_same_phase_requests_reuse_prefix_and_response_schema(self) -> None:
        planner = LLMPlanner("qa", "test-model", TestCharter(), 5.0, api_key="test-key")
        observation = {
            "player": {"present": True, "alive": True},
            "menu": {},
            "available_actions": ["direct_steer"],
        }
        transition = {
            "step": 0,
            "observation_id": "obs-0",
            "game_action": {
                "tool": "game",
                "action": "direct_steer",
                "arguments": {"x": 1.0, "y": 0.0, "duration": 5.0},
            },
            "observed_delta": {
                "before_observation_id": "obs-before",
                "after_observation_id": "obs-0",
                "changes": [],
            },
        }
        response = {"plan": "continue", "tool": "game", "action": "direct_steer"}

        with patch.object(planner, "_request", return_value=response) as request:
            planner.plan(observation, 1, [transition])
            first_call = request.call_args_list[-1]
            planner.commit_plan(response)
            planner.plan(observation, 2, [transition, {**transition, "step": 1}])
            second_call = request.call_args_list[-1]

        self.assertEqual(
            first_call.kwargs["response_schema"],
            second_call.kwargs["response_schema"],
        )
        self.assertEqual(
            first_call.kwargs["messages"][:3],
            second_call.kwargs["messages"][:3],
        )
        self.assertEqual(
            ["user", "assistant"],
            [message["role"] for message in second_call.kwargs["messages"][3:5]],
        )


class PlanningMetricsTests(unittest.TestCase):
    def test_uncached_prompt_tokens_are_derived_from_usage(self) -> None:
        usage = LLMPlanner._normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        )

        self.assertEqual(40, usage["uncached_prompt_tokens"])

    def test_api_usage_reports_planning_request_metrics(self) -> None:
        recorder = RunRecorder(
            output_dir=Path("/tmp/planning-metrics"),
            mode="qa",
            policy="llm",
            seed=1,
            planning_window_wall_seconds=10.0,
        )
        recorder.add_api_usage(
            "planning_request",
            {
                "prompt_tokens": 100,
                "uncached_prompt_tokens": 0,
                "cached_tokens": 100,
                "latency_ms": 500,
            },
            step=0,
            cache_boundary=True,
        )
        recorder.add_api_usage(
            "planning_request",
            {
                "prompt_tokens": 100,
                "uncached_prompt_tokens": 40,
                "cached_tokens": 60,
                "latency_ms": 2500,
            },
            step=1,
        )

        usage = recorder.api_usage_totals()

        self.assertEqual(40, usage["uncached_prompt_tokens"])
        self.assertEqual(40.0, usage["average_uncached_prompt_tokens"])
        self.assertEqual(0.6, usage["planning_cache_ratio"])
        self.assertEqual(1500.0, usage["planning_mean_latency_ms"])
        self.assertEqual(0.3, usage["llm_wait_ratio"])


if __name__ == "__main__":
    unittest.main()
