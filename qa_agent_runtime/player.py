"""Locate and validate a built Unity QA player on the current platform.

Unity ships a different shape per platform: macOS produces a ``.app`` bundle directory,
Linux an ``.x86_64`` executable, and Windows an ``.exe``. ``mlagents_envs`` already
resolves all three, so the only thing that has to be platform-aware here is our own
validation and the default path.
"""

from __future__ import annotations

from pathlib import Path
import plistlib
import sys

PLAYER_STEM = "QaGameplay"
DEFAULT_PLAYER_DIRECTORY = Path("QAArtifacts/player")

MACOS_SUFFIX = ".app"
LINUX_SUFFIX = ".x86_64"
WINDOWS_SUFFIX = ".exe"


class PlayerError(RuntimeError):
    """Raised when a Unity player is missing or does not match its platform."""


def player_suffix(platform: str | None = None) -> str:
    """Return the player suffix Unity produces for a platform."""
    name = platform or sys.platform
    if name == "darwin":
        return MACOS_SUFFIX
    if name.startswith("win"):
        return WINDOWS_SUFFIX
    return LINUX_SUFFIX


def default_player(directory: Path | None = None, platform: str | None = None) -> Path:
    """Return the conventional player path for a platform."""
    root = directory or DEFAULT_PLAYER_DIRECTORY
    return root / f"{PLAYER_STEM}{player_suffix(platform)}"


def validate_player(player: Path) -> Path:
    """Check that ``player`` exists in the shape its suffix implies.

    A macOS bundle is a directory and the other platforms ship a file, so checking
    existence alone would accept a bundle on Linux and an unpacked directory on Windows.
    """
    if player.suffix == MACOS_SUFFIX:
        if not player.is_dir():
            raise PlayerError(f"Unity player bundle does not exist: {player}")
        return player

    if player.suffix not in (LINUX_SUFFIX, WINDOWS_SUFFIX, ""):
        raise PlayerError(
            f"Unrecognized Unity player suffix {player.suffix!r}: expected "
            f"{MACOS_SUFFIX}, {LINUX_SUFFIX} or {WINDOWS_SUFFIX}."
        )
    if not player.is_file():
        raise PlayerError(f"Unity player executable does not exist: {player}")
    return player


def resolve_executable(player: Path) -> Path:
    """Return the executable launched directly by the Bridge client."""
    if player.suffix != MACOS_SUFFIX:
        return player

    plist_path = player / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as plist_file:
            plist = plistlib.load(plist_file)
    except (OSError, plistlib.InvalidFileException) as error:
        raise PlayerError(f"Unable to read macOS player Info.plist: {plist_path}") from error

    executable_name = plist.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise PlayerError(f"macOS player Info.plist is missing CFBundleExecutable: {plist_path}")

    executable = player / "Contents" / "MacOS" / executable_name
    if not executable.is_file():
        raise PlayerError(f"Unity player executable does not exist: {executable}")
    return executable


def artifact_root(player: Path) -> Path:
    """Return where the player writes its relative episode artifacts.

    Unity resolves a relative artifact directory against the process working directory,
    which for a launched player is the directory holding it on every platform.
    """
    return player.parent / "QAArtifacts"
