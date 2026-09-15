from __future__ import annotations

from pathlib import Path

from .common import PayloadAdapter


class ClaudeAdapter(PayloadAdapter):
    name = "claude"
    executable_name = "claude"

    def _instructions(self, destination: Path) -> tuple[str, ...]:
        return (
            f"/plugin marketplace add {destination}",
            "/plugin install nightfalcon@nightfalcon-local",
            "/nightfalcon:nightfalcon",
        )
