from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .bridge_client import BridgeClient
from .charter import DEFAULT_OBJECTIVE, TestCharter
from .planners import (
    HeuristicPlanner,
    LLMPlanner,
    Planner,
    build_action_contract,
    canonicalize_decision_arguments,
    compact_observation,
    observation_phase,
    validate_decision_against_contract,
)
from .reporting import RunRecorder
from .scenarios import ScenarioContractError, load_scenario
from .source_tools import SourceTools


SCENARIO_OWNED_ARGUMENTS = {
    "objective": "--objective",
    "movement_constraint": "--movement-constraint",
    "max_restarts": "--max-restarts",
    "focus_area": "--focus-area",
    "min_forward_component": "--min-forward-component",
    "collect_chests": "--collect-chests/--no-collect-chests",
    "chest_radius": "--chest-radius",
    "threat_radius": "--threat-radius",
    "survival_weight": "--survival-weight",
    "interrupt_health_ratio": "--interrupt-health-ratio",
    "interrupt_danger_score": "--interrupt-danger-score",
    "max_simulation_seconds": "--max-simulation-seconds",
    "max_steps": "--max-steps",
}

AD_HOC_DEFAULTS: dict[str, Any] = {
    "objective": DEFAULT_OBJECTIVE,
    "movement_constraint": "free",
    "max_restarts": 1,
    "focus_area": [],
    "seed": 1337,
    "min_forward_component": 0.15,
    "collect_chests": True,
    "chest_radius": 16.0,
    "threat_radius": 8.0,
    "survival_weight": 1.4,
    "interrupt_health_ratio": 0.30,
    "interrupt_danger_score": 0.85,
    "max_simulation_seconds": 60.0,
    "max_steps": 80,
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an online, pause/step gameplay QA smoke session.")
    parser.add_argument("--game-exe", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("player", "qa"), default="player")
    parser.add_argument("--policy", choices=("heuristic", "llm"), default="heuristic")
    parser.add_argument("--model", default=os.environ.get("QA_MODEL", ""))
    parser.add_argument("--api-url", default=os.environ.get("QA_API_URL"))
    parser.add_argument("--scenario")
    parser.add_argument("--objective", "--instruction", default=None)
    parser.add_argument(
        "--movement-constraint",
        choices=("free", "east", "west", "north", "south"),
        default=None,
    )
    parser.add_argument("--max-restarts", type=int, default=None)
    parser.add_argument("--focus-area", action="append", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument(
        "--plan-horizon-seconds", "--action-seconds", dest="plan_horizon_seconds",
        type=float, default=5.0,
        help="Maximum simulation-time horizon between LLM plans. Events can interrupt it early.",
    )
    parser.add_argument(
        "--pause-during-planning",
        action="store_true",
        help="Pause after each horizon instead of holding the previous LLM vector while the next plan is pending.",
    )
    parser.add_argument("--min-forward-component", type=float, default=None)
    chest_group = parser.add_mutually_exclusive_group()
    chest_group.add_argument("--collect-chests", dest="collect_chests", action="store_true")
    chest_group.add_argument("--no-collect-chests", dest="collect_chests", action="store_false")
    parser.set_defaults(collect_chests=None)
    parser.add_argument("--chest-radius", type=float, default=None)
    parser.add_argument("--threat-radius", type=float, default=None)
    parser.add_argument("--survival-weight", type=float, default=None)
    parser.add_argument("--interrupt-health-ratio", type=float, default=None)
    parser.add_argument("--interrupt-danger-score", type=float, default=None)
    parser.add_argument("--max-simulation-seconds", type=float, default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-source-steps", type=int, default=6)
    parser.add_argument(
        "--max-stalled-steps",
        type=int,
        default=2,
        help="Abort after this many consecutive LLM game actions make no observable progress.",
    )
    parser.add_argument("--headless", action="store_true", help="Suppress the game window. Visible window is the default.")
    parser.add_argument("--quiet", action="store_true", help="Suppress live plan/action console output.")
    return resolve_run_arguments(parser.parse_args(argv))


def resolve_run_arguments(args: argparse.Namespace) -> argparse.Namespace:
    args.scenario_definition = None
    args.preset = ""
    if args.scenario:
        conflicting = [
            flag
            for name, flag in SCENARIO_OWNED_ARGUMENTS.items()
            if getattr(args, name) is not None
        ]
        if conflicting:
            raise ScenarioContractError(
                f"scenario {args.scenario!r} owns its charter and limits; remove {conflicting[0]}"
            )
        scenario_path = args.project_root.resolve() / "config" / "qa-scenarios.json"
        scenario = load_scenario(args.scenario, scenario_path)
        args.scenario_definition = scenario
        args.preset = scenario.preset
        args.seed = scenario.select_seed(args.seed)
        charter_values = scenario.charter_definition.model_dump(mode="python")
        for name, value in charter_values.items():
            setattr(args, "focus_area" if name == "focus_areas" else name, value)
        args.max_simulation_seconds = scenario.limits.max_simulation_seconds
        args.max_steps = scenario.limits.max_steps
        return args

    for name, value in AD_HOC_DEFAULTS.items():
        if getattr(args, name) is None:
            setattr(args, name, list(value) if isinstance(value, list) else value)
    return args


def charter_from_args(args: argparse.Namespace) -> TestCharter:
    return TestCharter(
        objective=args.objective,
        movement_constraint=args.movement_constraint,
        max_restarts=args.max_restarts,
        focus_areas=tuple(args.focus_area),
        min_forward_component=args.min_forward_component,
        collect_chests=args.collect_chests,
        chest_radius=args.chest_radius,
        threat_radius=args.threat_radius,
        survival_weight=args.survival_weight,
        interrupt_health_ratio=args.interrupt_health_ratio,
        interrupt_danger_score=args.interrupt_danger_score,
    )


def terminal_stop_reason(
    observation: dict[str, Any], restarts_used: int, max_restarts: int
) -> str | None:
    if observation_phase(observation) == "game_over" and restarts_used >= max_restarts:
        return (
            "Game over reached after the configured restart budget was exhausted "
            f"({restarts_used}/{max_restarts})."
        )
    return None


def attach_decision_identity(
    decision: dict[str, Any], run_id: str, step: int
) -> dict[str, Any]:
    identified = dict(decision)
    identified["decision_id"] = f"{run_id}-decision-{step:08d}"
    return identified


def normalize_decision(
    decision: dict[str, Any],
    mode: str,
    charter: TestCharter,
    action_seconds: float = 5.0,
    restarts_used: int = 0,
    llm_direct_control: bool = False,
    continue_during_planning: bool = True,
) -> dict[str, Any]:
    tool = str(decision.get("tool", "game")).lower()
    action = str(decision.get("action", "observe"))
    enforcements: list[dict[str, Any]] = []
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if mode == "player" and tool != "game":
        tool, action, arguments = "game", "observe", {}
    if tool == "game":
        allowed = {
            "observe", "start_game", "direct_steer", "steer", "move", "wait", "select_upgrade", "use_item",
            "pause", "restart", "return_to_menu", "shutdown",
        }
        if action not in allowed:
            action, arguments = "observe", {}
        if llm_direct_control and action in ("steer", "move"):
            enforcements.append(
                {
                    "constraint": "llm_direct_control",
                    "requested": action,
                    "executed": "direct_steer",
                    "reason": "Hybrid bridge steering is disabled for LLM capability evaluation.",
                }
            )
            action = "direct_steer"
        try:
            if action == "direct_steer":
                arguments = {
                    "x": float(arguments.get("x", 0.0)),
                    "y": float(arguments.get("y", 0.0)),
                    "duration": min(max(float(arguments.get("duration", 1.0)), 0.25), action_seconds),
                    "intent": str(arguments.get("intent", "unspecified"))[:80],
                    "target_id": int(arguments.get("target_id", 0)),
                    "threat_radius": charter.threat_radius,
                    "interrupt_health_ratio": charter.interrupt_health_ratio,
                    "interrupt_danger_score": charter.interrupt_danger_score,
                    "interrupt_chest_distance": 2.5,
                    "continue_during_planning": bool(llm_direct_control and continue_during_planning),
                }
            elif action in ("steer", "move"):
                arguments = {
                    "x": float(arguments.get("x", 0.0)),
                    "y": float(arguments.get("y", 0.0)),
                    "duration": min(max(float(arguments.get("duration", 1.0)), 0.05), action_seconds),
                }
                constrained_vector = charter.movement_vector
                if constrained_vector is not None:
                    requested = {"x": arguments["x"], "y": arguments["y"]}
                    if action == "steer":
                        arguments["x"], arguments["y"] = constrained_vector
                    else:
                        arguments["x"], arguments["y"] = _enforce_forward_component(
                            arguments["x"], arguments["y"], constrained_vector, charter.min_forward_component
                        )
                    if requested != {"x": arguments["x"], "y": arguments["y"]}:
                        enforcements.append(
                            {
                                "constraint": "movement",
                                "requested": requested,
                                "executed": {"x": arguments["x"], "y": arguments["y"]},
                            }
                        )
                if action == "steer":
                    arguments.update(
                        {
                            "collect_chests": charter.collect_chests,
                            "chest_radius": charter.chest_radius,
                            "threat_radius": charter.threat_radius,
                            "survival_weight": charter.survival_weight,
                            "min_forward_component": charter.min_forward_component,
                            "interrupt_health_ratio": charter.interrupt_health_ratio,
                            "interrupt_danger_score": charter.interrupt_danger_score,
                        }
                    )
            elif action == "wait":
                arguments = {"duration": min(max(float(arguments.get("duration", 1.0)), 0.05), action_seconds)}
            elif action in ("start_game", "select_upgrade", "use_item"):
                arguments = {"index": int(arguments.get("index", 0))}
            elif action == "restart" and restarts_used >= charter.max_restarts:
                enforcements.append(
                    {
                        "constraint": "max_restarts",
                        "requested": "restart",
                        "executed": "observe",
                    }
                )
                action, arguments = "observe", {}
            else:
                arguments = {}
        except (TypeError, ValueError):
            action, arguments = "observe", {}
    return {
        "plan": str(decision.get("plan", "")),
        "hypothesis": str(decision.get("hypothesis", "")),
        "tool": tool,
        "action": action,
        "arguments": arguments,
        "constraint_enforcements": enforcements,
        "syntax_normalizations": list(decision.get("_syntax_normalizations") or []),
    }


def attach_navigation_evaluation_context(
    decision: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    """Snapshot pre-action geometry so the report can score what the model actually chose."""
    if decision.get("tool") != "game" or decision.get("action") != "direct_steer":
        return decision
    arguments = decision.get("arguments") or {}
    world = observation.get("world") or {}
    target_id = int(arguments.get("target_id", 0) or 0)
    target = next(
        (
            item for item in (world.get("visible_chests") or [])
            if isinstance(item, dict) and int(item.get("id", 0) or 0) == target_id
        ),
        None,
    )
    decision["evaluation_context"] = {
        "pre_level_time": float(((observation.get("progress") or {}).get("level_time") or 0.0)),
        "pre_player_position": (observation.get("player") or {}).get("position") or {},
        "pre_danger_score": float(world.get("danger_score") or 0.0),
        "escape_vector": world.get("escape_vector") or {},
        "target_chest": target,
    }
    return decision


def _enforce_forward_component(
    x: float,
    y: float,
    heading: tuple[float, float],
    minimum: float,
) -> tuple[float, float]:
    magnitude = math.hypot(x, y)
    if magnitude < 1e-6:
        return heading
    x, y = x / magnitude, y / magnitude
    dot = x * heading[0] + y * heading[1]
    if dot >= minimum:
        return round(x, 6), round(y, 6)
    lateral_x = x - dot * heading[0]
    lateral_y = y - dot * heading[1]
    lateral_magnitude = math.hypot(lateral_x, lateral_y)
    if lateral_magnitude < 1e-6:
        return heading
    lateral_scale = math.sqrt(max(0.0, 1.0 - minimum * minimum)) / lateral_magnitude
    return (
        round(heading[0] * minimum + lateral_x * lateral_scale, 6),
        round(heading[1] * minimum + lateral_y * lateral_scale, 6),
    )


def execute_source_tool(tools: SourceTools, decision: dict[str, Any]) -> dict[str, Any]:
    arguments = decision["arguments"]
    if decision["tool"] == "source_search":
        return tools.search(str(arguments.get("query", "")))
    return tools.read(
        str(arguments.get("path", "")),
        int(arguments.get("line_start", 1)),
        int(arguments.get("line_count", 120)),
    )


def merge_api_usage(*items: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            try:
                merged[key] = merged.get(key, 0) + max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
    return merged


def observation_made_progress(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Treat simulation, navigation, phase, or live-event changes as observable progress."""
    if observation_phase(before) != observation_phase(after):
        return True
    if before.get("scene") != after.get("scene"):
        return True
    before_time = float(((before.get("progress") or {}).get("level_time") or 0.0))
    after_time = float(((after.get("progress") or {}).get("level_time") or 0.0))
    if after_time > before_time + 0.01:
        return True
    before_position = (before.get("player") or {}).get("position") or {}
    after_position = (after.get("player") or {}).get("position") or {}
    dx = float(after_position.get("x", 0.0) or 0.0) - float(before_position.get("x", 0.0) or 0.0)
    dy = float(after_position.get("y", 0.0) or 0.0) - float(before_position.get("y", 0.0) or 0.0)
    if math.hypot(dx, dy) > 0.01:
        return True
    if (after.get("event_state") or {}).get("type"):
        return True
    return (before.get("menu") or {}) != (after.get("menu") or {})


def record_contract_event(output_dir: Path, event: dict[str, Any]) -> None:
    with (output_dir / "llm-contract-events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_session(args: argparse.Namespace) -> int:
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    scenario_id = str(getattr(args, "scenario", "") or "")
    try:
        charter = charter_from_args(args)
    except Exception as error:
        print(json.dumps({"result": "fail", "error": f"Invalid test charter: {error}"}, ensure_ascii=False))
        return 2
    recorder = RunRecorder(
        output_dir,
        args.mode,
        args.policy,
        args.seed,
        run_id=run_id,
        charter=charter.as_dict(),
        model=args.model if args.policy == "llm" else None,
        game_window_visible=not args.headless,
    )
    planner: Planner
    try:
        if args.policy == "llm":
            planner = LLMPlanner(args.mode, args.model, charter, args.plan_horizon_seconds, args.api_url)
        else:
            planner = HeuristicPlanner(args.plan_horizon_seconds, charter)
    except Exception as error:
        report = recorder.build_report(None, f"{type(error).__name__}: {error}")
        recorder.write_report(report)
        print(json.dumps({"result": "fail", "report": str(output_dir / "report.json")}, ensure_ascii=False))
        return 1

    source_tools = SourceTools(args.project_root)
    tool_context: list[dict[str, Any]] = []
    source_steps = 0
    fatal_error: str | None = None
    last_observation: dict[str, Any] = {}
    started = time.monotonic()
    client = BridgeClient(
        args.game_exe,
        output_dir,
        args.mode,
        args.seed,
        args.time_scale,
        args.headless,
        run_id=run_id,
        scenario_id=scenario_id,
        preset=getattr(args, "preset", ""),
    )
    restarts_used = 0
    stalled_steps = 0

    try:
        if not args.quiet:
            visibility = "headless" if args.headless else "visible 1280x720 window"
            print(f"[launch] Starting Unity player in {visibility}.", flush=True)
            print(f"[charter] {charter.objective}", flush=True)
            print(
                f"[charter targets] heading={charter.movement_constraint}, requested_min_forward={charter.min_forward_component}, "
                f"collect_chests={charter.collect_chests}, max_restarts={charter.max_restarts}; "
                f"llm_direction_correction={'disabled' if args.policy == 'llm' else 'baseline-specific'}",
                flush=True,
            )
        ready = client.launch()
        recorder.launched = True
        serialized_arguments = {
            key: value
            for key, value in vars(args).items()
            if key != "scenario_definition"
        }
        serialized_arguments.update(
            {
                "game_exe": str(args.game_exe),
                "project_root": str(args.project_root),
                "output": str(args.output),
            }
        )
        (output_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "arguments": serialized_arguments,
                    "ready": ready,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        last_observation = client.command(
            "observe", decision_id=f"{run_id}-decision-bootstrap"
        )
        for step in range(args.max_steps):
            stop_reason = terminal_stop_reason(last_observation, restarts_used, charter.max_restarts)
            if stop_reason:
                if not args.quiet:
                    print(f"[terminal] {stop_reason}", flush=True)
                break
            if not args.quiet and args.policy == "llm":
                print(f"[step {step:02d}] Waiting for LLM plan...", flush=True)
            source_steps_remaining = max(0, args.max_source_steps - source_steps)
            raw_decision = canonicalize_decision_arguments(
                planner.plan(last_observation, step, tool_context, source_steps_remaining)
            )
            planning_usage = planner.take_last_usage()
            if raw_decision.get("_syntax_normalizations"):
                record_contract_event(
                    output_dir,
                    {
                        "step": step,
                        "kind": "argument_syntax_normalized",
                        "normalizations": raw_decision["_syntax_normalizations"],
                        "decision": raw_decision,
                    },
                )
            if args.policy == "llm":
                contract = build_action_contract(
                    last_observation, args.mode, charter, source_steps_remaining
                )
                contract_error = validate_decision_against_contract(raw_decision, contract)
                if contract_error:
                    record_contract_event(
                        output_dir,
                        {
                            "step": step,
                            "kind": "initial_contract_rejection",
                            "error": contract_error,
                            "decision": raw_decision,
                            "contract": contract,
                        },
                    )
                    if not args.quiet:
                        print(
                            f"[step {step:02d}] LLM action rejected: {contract_error}. Requesting one correction...",
                            flush=True,
                        )
                    if not isinstance(planner, LLMPlanner):
                        raise RuntimeError(contract_error)
                    repaired_decision = canonicalize_decision_arguments(
                        planner.repair_plan(
                            last_observation,
                            step,
                            tool_context,
                            raw_decision,
                            contract_error,
                            source_steps_remaining,
                        )
                    )
                    if repaired_decision.get("_syntax_normalizations"):
                        record_contract_event(
                            output_dir,
                            {
                                "step": step,
                                "kind": "repair_argument_syntax_normalized",
                                "normalizations": repaired_decision["_syntax_normalizations"],
                                "decision": repaired_decision,
                            },
                        )
                    planning_usage = merge_api_usage(planning_usage, planner.take_last_usage())
                    repair_error = validate_decision_against_contract(repaired_decision, contract)
                    if repair_error:
                        record_contract_event(
                            output_dir,
                            {
                                "step": step,
                                "kind": "repair_contract_rejection",
                                "error": repair_error,
                                "decision": repaired_decision,
                                "contract": contract,
                            },
                        )
                        recorder.add_api_usage("planning_rejected", planning_usage, step)
                        recorder.anomalies.append(
                            {
                                "step": step,
                                "kind": "llm_action_contract_failure",
                                "severity": "high",
                                "evidence": repair_error,
                            }
                        )
                        raise RuntimeError(
                            "LLM repeated an invalid action after one corrective retry: " + repair_error
                        )
                    raw_decision = repaired_decision
            decision = normalize_decision(
                raw_decision,
                args.mode,
                charter,
                args.plan_horizon_seconds,
                restarts_used,
                args.policy == "llm",
                not args.pause_during_planning,
            )
            decision = attach_decision_identity(decision, run_id, step)
            attach_navigation_evaluation_context(decision, last_observation)
            if not args.quiet:
                arguments_text = json.dumps(decision["arguments"], ensure_ascii=False)
                print(
                    f"[step {step:02d}] {decision['tool']}.{decision['action']} {arguments_text} | {decision['plan']}",
                    flush=True,
                )
            if decision["tool"] in ("source_search", "source_read"):
                if args.mode != "qa" or source_steps >= args.max_source_steps:
                    result = {"ok": False, "error": "Source tool budget exhausted or unavailable in Player mode"}
                else:
                    result = execute_source_tool(source_tools, decision)
                    source_steps += 1
                tool_context.append({"decision": decision, "result": result})
                recorder.record(step, decision, last_observation, time.monotonic() - started, planning_usage)
                continue
            if decision["tool"] != "game":
                decision = attach_decision_identity(
                    {"plan": "Recover from invalid tool.", "hypothesis": "", "tool": "game", "action": "observe", "arguments": {}},
                    run_id,
                    step,
                )

            previous_observation = last_observation
            last_observation = client.command(
                decision["action"],
                decision_id=decision["decision_id"],
                **decision["arguments"],
            )
            if decision["action"] == "restart" and last_observation.get("ok"):
                restarts_used += 1
            recorder.record(step, decision, last_observation, time.monotonic() - started, planning_usage)
            if args.policy == "llm" and observation_phase(previous_observation) == "active_gameplay":
                if observation_made_progress(previous_observation, last_observation):
                    stalled_steps = 0
                else:
                    stalled_steps += 1
                    if not args.quiet:
                        print(
                            f"[stall] No observable game change ({stalled_steps}/{args.max_stalled_steps}).",
                            flush=True,
                        )
                    if stalled_steps >= max(1, args.max_stalled_steps):
                        recorder.anomalies.append(
                            {
                                "step": step,
                                "kind": "llm_gameplay_stalled",
                                "severity": "high",
                                "evidence": (
                                    f"{stalled_steps} consecutive LLM game actions changed neither "
                                    "simulation time, player position, phase, menu, nor event state."
                                ),
                            }
                        )
                        raise RuntimeError(
                            "LLM gameplay stalled; stopped before spending more API credit"
                        )
            event_state = last_observation.get("event_state") or {}
            if not args.quiet and event_state.get("type"):
                print(
                    f"[event] {event_state.get('type')}: {event_state.get('detail', '')}",
                    flush=True,
                )
            world = last_observation.get("world") or {}
            tool_context.append(
                {
                    "game_action": decision,
                    "result": last_observation.get("result"),
                    "scene": last_observation.get("scene"),
                    "progress": last_observation.get("progress"),
                    "menu": last_observation.get("menu"),
                    "event_state": event_state,
                    "controller": last_observation.get("controller"),
                    "navigation": {
                        "danger_score": world.get("danger_score"),
                        "nearby_enemy_count": world.get("nearby_enemy_count"),
                        "nearest_chest_distance": world.get("nearest_chest_distance"),
                        "nearest_chest_vector": world.get("nearest_chest_vector"),
                    },
                }
            )
            tool_context[:] = tool_context[-6:]
            if recorder.total_simulation_time >= args.max_simulation_seconds:
                break
    except Exception as error:
        fatal_error = f"{type(error).__name__}: {error}"
    finally:
        client.close()

    assessment_context = {
        "mode": args.mode,
        "policy": args.policy,
        "test_charter": charter.as_dict(),
        "charter_compliance": recorder.charter_compliance(),
        "metrics": {
            "steps": len(recorder.steps),
            "total_simulation_time": recorder.total_simulation_time,
            "max_level_time": recorder.max_level_time,
            "max_level": recorder.max_level,
            "max_kills": recorder.max_kills,
            "event_counts": recorder.event_counts,
            "chest_collections": recorder.chest_collections,
            "direct_control_horizons": recorder.direct_control_horizons,
            "continuous_control_horizons": recorder.continuous_control_horizons,
            "planner_intent_counts": recorder.intent_counts,
            "chest_navigation": recorder.chest_navigation_metrics(),
            "api_usage": recorder.api_usage_totals(),
        },
        "anomalies": recorder.anomalies,
        "last_observation": compact_observation(last_observation),
        "fatal_error": fatal_error,
    }
    llm_assessment = None
    if fatal_error is None and args.policy == "llm":
        try:
            llm_assessment = planner.final_assessment(assessment_context)
            recorder.add_api_usage("final_assessment", planner.take_last_usage())
        except Exception as error:
            recorder.anomalies.append({"step": len(recorder.steps), "kind": "llm_report_failed", "severity": "medium", "evidence": str(error)})
    report = recorder.build_report(llm_assessment, fatal_error)
    recorder.write_report(report)
    if not args.quiet:
        print(f"[report] {output_dir / 'report.md'}", flush=True)
    print(json.dumps({"result": report["result"], "report": str(output_dir / "report.json")}, ensure_ascii=False))
    return 0 if report["result"] == "pass" else 1


def main() -> None:
    try:
        args = parse_args()
    except ScenarioContractError as error:
        print(json.dumps({"result": "contract_error", "error": str(error)}, ensure_ascii=False))
        sys.exit(2)
    sys.exit(run_session(args))


if __name__ == "__main__":
    main()
