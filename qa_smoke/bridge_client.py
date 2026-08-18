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


class BridgeClient:
    def __init__(
        self,
        game_exe: Path,
        session_dir: Path,
        mode: str,
        seed: int,
        time_scale: float,
        headless: bool = False,
        launch_timeout: float = 45.0,
    ) -> None:
        self.game_exe = game_exe.resolve()
        self.session_dir = session_dir.resolve()
        self.bridge_dir = self.session_dir / "bridge"
        self.mode = mode
        self.seed = seed
        self.time_scale = time_scale
        self.headless = headless
        self.launch_timeout = launch_timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout = None

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
            "-logFile",
            str(self.session_dir / "unity-player.log"),
            "-screen-width",
            "1280",
            "-screen-height",
            "720",
            "-screen-fullscreen",
            "0",
        ]
        if self.headless:
            args.extend(["-batchmode", "-nographics"])
        self.process = subprocess.Popen(args, stdout=self._stdout, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.launch_timeout
        while time.monotonic() < deadline:
            self._assert_running()
            payload = self._read_json(self.ready_path)
            if payload and payload.get("ready"):
                return payload
            time.sleep(0.1)
        raise BridgeError(f"Timed out waiting for QA Bridge ready file: {self.ready_path}")

    def command(self, action: str, timeout: float = 45.0, **parameters: Any) -> dict[str, Any]:
        self._assert_running()
        command_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {"id": command_id, "action": action}
        payload.update(parameters)
        self._write_json_atomic(self.command_path, payload)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._assert_running()
            observation = self._read_json(self.response_path(command_id))
            if observation and observation.get("command_id") == command_id:
                return observation
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
