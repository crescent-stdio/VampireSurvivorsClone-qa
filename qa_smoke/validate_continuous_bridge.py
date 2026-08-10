from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .bridge_client import BridgeClient


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    output = root / "SmokeRuns" / f"continuous-bridge-validation-{datetime.now():%Y%m%d-%H%M%S}"
    client = BridgeClient(
        root / "Build" / "QASmoke" / "VampireSurvivorsClone.exe",
        output,
        "qa",
        1337,
        1.0,
        headless=False,
    )
    try:
        ready = client.launch()
        client.command("start_game", index=0)
        horizon = client.command(
            "direct_steer",
            x=1.0,
            y=0.0,
            duration=0.75,
            intent="continuous_bridge_validation",
            target_id=0,
            threat_radius=8.0,
            interrupt_health_ratio=0.3,
            interrupt_danger_score=0.95,
            interrupt_chest_distance=1.0,
            continue_during_planning=True,
        )
        first_time = float((horizon.get("progress") or {}).get("level_time") or 0.0)
        first_x = float((((horizon.get("player") or {}).get("position") or {}).get("x")) or 0.0)
        time.sleep(0.8)
        during_planning = client.command("observe")
        second_time = float((during_planning.get("progress") or {}).get("level_time") or 0.0)
        second_x = float((((during_planning.get("player") or {}).get("position") or {}).get("x")) or 0.0)
        summary = {
            "output": str(output),
            "protocol_version": ready.get("protocol_version"),
            "horizon_paused": horizon.get("paused"),
            "horizon_controller_active": (horizon.get("controller") or {}).get("active"),
            "planning_observation_paused": during_planning.get("paused"),
            "level_time_advanced_while_planning": round(second_time - first_time, 4),
            "x_advanced_while_planning": round(second_x - first_x, 4),
        }
        print(json.dumps(summary, indent=2))
        if horizon.get("paused") or not (horizon.get("controller") or {}).get("active"):
            raise RuntimeError("Direct-control horizon did not enter continuous planning hold")
        if second_time <= first_time + 0.25 or second_x <= first_x + 0.25:
            raise RuntimeError("Gameplay did not continue on the previous LLM vector during planning latency")
        client.command("pause")
    finally:
        client.close()


if __name__ == "__main__":
    main()

