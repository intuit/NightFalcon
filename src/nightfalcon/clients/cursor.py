from __future__ import annotations

from pathlib import Path

from .base import ClientInstallError, InstallPlan
from .common import PayloadAdapter


class CursorAdapter(PayloadAdapter):
    name = "cursor"
    executable_name = "cursor"

    def plan(self, source: Path, destination: Path) -> InstallPlan:
        if destination == Path(".") or not destination.is_absolute():
            raise ClientInstallError("Cursor installation requires an absolute target")
        return super().plan(source, destination)

    def _instructions(self, destination: Path) -> tuple[str, ...]:
        return (
            f"Open {destination} as the Cursor workspace",
            "/nightfalcon",
        )
