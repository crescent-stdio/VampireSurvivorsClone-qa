from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
import uuid

from .bridge_client import BridgeClient
from .memory import sanitize_error_type


ExitKind = Literal["normal", "crash", "timeout", "error"]


@dataclass(frozen=True)
class EpisodeExit:
    kind: ExitKind
    return_code: int | None = None
    detail: str = ""


class GameAdapter(Protocol):
    def start(self, *, seed: int, preset: str, faults: list[str]) -> None: ...

    def read_state(self) -> dict[str, Any]: ...

    def send_input(self, action: dict[str, Any]) -> dict[str, Any]: ...

    def stop(self) -> EpisodeExit: ...


BridgeFactory = Callable[..., Any]


class VampireSurvivorsAdapter:
    """Game-specific adapter around the existing file bridge.

    The adapter intentionally keeps the BridgeClient contract intact. This makes
    the runner replaceable while allowing legacy smoke sessions to keep using
    the bridge directly.
    """

    def __init__(
        self,
        *,
        game_exe: Path,
        session_dir: Path,
        mode: str = "qa",
        time_scale: float = 1.0,
        headless: bool = False,
        run_id: str | None = None,
        scenario_id: str = "",
        bridge_factory: BridgeFactory = BridgeClient,
    ) -> None:
        self.game_exe = game_exe
        self.session_dir = session_dir
        self.mode = mode
        self.time_scale = time_scale
        self.headless = headless
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.bridge_factory = bridge_factory
        self._bridge: Any | None = None
        self._last_state: dict[str, Any] = {}

    def start(self, *, seed: int, preset: str, faults: list[str]) -> dict[str, Any]:
        if len(faults) > 1:
            raise ValueError("an episode accepts at most one fault")
        if self._bridge is not None:
            raise RuntimeError("adapter episode is already running")
        self._bridge = self.bridge_factory(
            game_exe=self.game_exe,
            session_dir=self.session_dir,
            mode=self.mode,
            seed=seed,
            time_scale=self.time_scale,
            headless=self.headless,
            run_id=self.run_id or uuid.uuid4().hex,
            scenario_id=self.scenario_id,
            preset=preset,
            fault_id=faults[0] if faults else "",
        )
        ready = self._bridge.launch()
        self._last_state = {}
        return ready

    def read_state(self) -> dict[str, Any]:
        return self._last_state

    def send_input(self, action: dict[str, Any]) -> dict[str, Any]:
        if self._bridge is None:
            raise RuntimeError("adapter episode has not been started")
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise ValueError("action must contain a string 'action' field")
        payload = dict(action)
        action_name = payload.pop("action")
        self._last_state = self._bridge.command(action_name, **payload)
        return self._last_state

    def launch(self) -> dict[str, Any]:
        """Compatibility entry point for callers that predate the adapter contract."""

        return self.start(seed=0, preset="", faults=[])

    def command(self, action: str, **parameters: Any) -> dict[str, Any]:
        payload = {"action": action, **parameters}
        return self.send_input(payload)

    def stop(self) -> EpisodeExit:
        if self._bridge is None:
            return EpisodeExit(kind="normal")
        bridge = self._bridge
        process = getattr(bridge, "process", None)
        return_code = getattr(process, "returncode", None)
        try:
            bridge.close()
        except TimeoutError as error:
            error_type = sanitize_error_type(type(error).__name__) or "Exception"
            return EpisodeExit(kind="timeout", return_code=return_code, detail=error_type)
        except Exception as error:  # pragma: no cover - defensive bridge boundary
            error_type = sanitize_error_type(type(error).__name__) or "Exception"
            kind: ExitKind = "timeout" if error_type == "TimeoutExpired" else "error"
            return EpisodeExit(kind=kind, return_code=return_code, detail=error_type)
        finally:
            self._bridge = None
        if return_code not in (None, 0):
            return EpisodeExit(kind="crash", return_code=return_code)
        return EpisodeExit(kind="normal", return_code=return_code)

    def close(self) -> None:
        self.stop()
