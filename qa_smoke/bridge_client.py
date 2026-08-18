from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from qa_agent_runtime.player import PlayerError, resolve_executable, validate_player


class BridgeError(RuntimeError):
    pass


class BridgeContractError(BridgeError):
    pass


class BridgeClient:
    PROTOCOL_VERSION = "1.4"

    def __init__(
        self,
        game_exe: Path,
        session_dir: Path,
        mode: str,
        seed: int,
        time_scale: float,
        headless: bool = False,
        launch_timeout: float = 45.0,
        run_id: str | None = None,
        scenario_id: str = "",
        preset: str = "",
    ) -> None:
        self.game_exe = game_exe.resolve()
        self.session_dir = session_dir.resolve()
        self.bridge_dir = self.session_dir / "bridge"
        self.mode = mode
        self.seed = seed
        self.time_scale = time_scale
        self.headless = headless
        self.launch_timeout = launch_timeout
        self.run_id = run_id or uuid.uuid4().hex
        self.scenario_id = scenario_id
        self.preset = preset
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout = None
        self._issued_command_ids: set[str] = set()
        self._observation_ids: set[str] = set()

    @property
    def command_path(self) -> Path:
        return self.bridge_dir / "command.json"

    @property
    def response_directory(self) -> Path:
        return self.bridge_dir / "responses"

    def response_path(self, command_id: str) -> Path:
        return self.response_directory / f"{command_id}.json"

    @property
    def ready_path(self) -> Path:
        return self.bridge_dir / "ready.json"

    def launch(self) -> dict[str, Any]:
        try:
            self.game_exe = resolve_executable(validate_player(self.game_exe))
        except PlayerError as error:
            raise BridgeError(str(error)) from error
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.response_directory.mkdir(parents=True, exist_ok=True)
        self._stdout = (self.session_dir / "game-console.log").open("wb")
        args = [
            str(self.game_exe),
            "-qaBridgeDir",
            str(self.bridge_dir),
            "-qaMode",
            self.mode,
            "-qaSeed",
            str(self.seed),
            "-qaTimeScale",
            str(self.time_scale),
            f"-qaRunId={self.run_id}",
            f"-qaScenarioId={self.scenario_id}",
            "-logFile",
            str(self.session_dir / "unity-player.log"),
            "-screen-width",
            "1280",
            "-screen-height",
            "720",
            "-screen-fullscreen",
            "0",
        ]
        if self.preset:
            args.append(f"-qaPreset={self.preset}")
        if self.headless:
            args.extend(["-batchmode", "-nographics"])
        self.process = subprocess.Popen(args, stdout=self._stdout, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.launch_timeout
        while time.monotonic() < deadline:
            self._assert_running()
            payload = self._read_json(self.ready_path)
            if payload and payload.get("ready"):
                return self._validate_ready(payload)
            time.sleep(0.1)
        raise BridgeError(f"Timed out waiting for QA Bridge ready file: {self.ready_path}")

    def command(self, action: str, timeout: float = 45.0, **parameters: Any) -> dict[str, Any]:
        self._assert_running()
        command_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {"id": command_id, "action": action}
        payload.update(parameters)
        self._issued_command_ids.add(command_id)
        self._write_json_atomic(self.command_path, payload)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._assert_running()
            observation = self._read_json(self.response_path(command_id))
            if observation and observation.get("command_id") == command_id:
                return self._validate_observation(observation, command_id)
            time.sleep(0.03)
        raise BridgeError(f"Timed out waiting for response to {action} ({command_id})")

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.command("shutdown", timeout=5.0)
            except Exception:
                self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        if self._stdout is not None:
            self._stdout.close()
            self._stdout = None

    def _assert_running(self) -> None:
        if self.process is None:
            raise BridgeError("Game process has not been launched")
        return_code = self.process.poll()
        if return_code is not None:
            raise BridgeError(f"Game process exited unexpectedly with code {return_code}")

    def _validate_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_envelope(payload, "ready")
        return payload

    def _validate_observation(
        self, payload: dict[str, Any], command_id: str
    ) -> dict[str, Any]:
        self._validate_envelope(payload, "observation")
        if payload.get("command_id") != command_id:
            raise BridgeContractError(
                f"observation command_id mismatch: expected {command_id!r}, "
                f"received {payload.get('command_id')!r}"
            )
        observation_id = payload.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            raise BridgeContractError("observation_id must be a non-empty string")
        if observation_id in self._observation_ids:
            raise BridgeContractError(f"duplicate observation_id: {observation_id}")
        self._observation_ids.add(observation_id)

        event = payload.get("event_state") or {}
        if event.get("type"):
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise BridgeContractError("event_id must be present for event evidence")
            caused_by = event.get("caused_by_command_id")
            if caused_by not in self._issued_command_ids:
                raise BridgeContractError(
                    f"orphan event evidence references unknown command_id: {caused_by!r}"
                )
        return payload

    def _validate_envelope(self, payload: dict[str, Any], kind: str) -> None:
        if payload.get("protocol_version") != self.PROTOCOL_VERSION:
            raise BridgeContractError(
                f"{kind} protocol_version mismatch: expected {self.PROTOCOL_VERSION!r}, "
                f"received {payload.get('protocol_version')!r}"
            )
        if payload.get("run_id") != self.run_id:
            raise BridgeContractError(
                f"{kind} run_id mismatch: expected {self.run_id!r}, "
                f"received {payload.get('run_id')!r}"
            )
        if payload.get("scenario_id", "") != self.scenario_id:
            raise BridgeContractError(
                f"{kind} scenario_id mismatch: expected {self.scenario_id!r}, "
                f"received {payload.get('scenario_id')!r}"
            )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return None

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def __enter__(self) -> "BridgeClient":
        self.launch()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
