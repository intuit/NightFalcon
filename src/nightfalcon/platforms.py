from __future__ import annotations

from collections.abc import Mapping
import ntpath
from pathlib import Path


def _absolute_directory(value: str | None, variable: str, platform: str) -> Path:
    if not value:
        raise ValueError(f"{variable} must be set")
    is_absolute = ntpath.isabs(value) if platform == "win32" else Path(value).is_absolute()
    if not is_absolute:
        raise ValueError(f"{variable} must be an absolute path")
    return Path(value)


def data_root(env: Mapping[str, str], platform: str) -> Path:
    """Return NightFalcon-owned user data root without creating it."""
    if platform == "darwin":
        home = _absolute_directory(env.get("HOME"), "HOME", platform)
        return home / "Library" / "Application Support" / "NightFalcon"
    if platform == "linux":
        if env.get("XDG_DATA_HOME"):
            base = _absolute_directory(env["XDG_DATA_HOME"], "XDG_DATA_HOME", platform)
        else:
            base = _absolute_directory(env.get("HOME"), "HOME", platform) / ".local/share"
        return base / "nightfalcon"
    if platform == "win32":
        base = _absolute_directory(env.get("LOCALAPPDATA"), "LOCALAPPDATA", platform)
        return base / "NightFalcon"
    raise ValueError(f"unsupported platform: {platform}")
