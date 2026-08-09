import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_player(tmp_path: Path) -> Path:
    bundle = tmp_path / "QaGameplay.app"
    executable = bundle / "Contents" / "MacOS" / "project_mgd_vampire"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return bundle


def make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    capture = tmp_path / "arguments"
    stub = tmp_path / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = lock ]; then exit 0; fi\n"
        "printf '%s\\n' \"$@\" >\"$QA_CAPTURE\"\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, capture


def run_script(script: str, args: list[str], tmp_path: Path, *, default_player: bool = False):
    player = make_player(tmp_path)
    uv, capture = make_uv_stub(tmp_path)
    environment = os.environ.copy()
    environment.update({"UV_BIN": str(uv), "QA_CAPTURE": str(capture)})
    if default_player:
        environment["QA_PLAYER"] = str(player)
    else:
        args = [str(player), *args]
    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "scripts" / "qa" / script), *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, capture.read_text(encoding="utf-8").splitlines()


def test_train_pytorch_script_passes_the_player_device_and_remaining_arguments(tmp_path: Path) -> None:
    result, arguments = run_script(
        "train-pytorch.sh",
        ["--total-steps", "8"],
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert arguments == [
        "run", "--locked", "--extra", "trainer", "python", "-m", "qa_pytorch_ppo.cli",
        "train", "--player", str(tmp_path / "QaGameplay.app"), "--device", "cpu",
        "--total-steps", "8",
    ]


def test_evaluate_pytorch_script_accepts_options_without_a_positional_player(tmp_path: Path) -> None:
    result, arguments = run_script(
        "evaluate-pytorch.sh",
        ["--seed", "1234"],
        tmp_path,
        default_player=True,
    )

    assert result.returncode == 0, result.stderr
    assert arguments[-6:] == [
        "--player", str(tmp_path / "QaGameplay.app"), "--device", "cpu", "--seed", "1234"
    ]
