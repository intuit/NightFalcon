"""Client-specific NightFalcon installers."""

from .base import ClientAdapter, ClientInstallError, Detection, InstallPlan
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter

__all__ = [
    "ClaudeAdapter",
    "ClientAdapter",
    "ClientInstallError",
    "CodexAdapter",
    "CursorAdapter",
    "Detection",
    "InstallPlan",
]
