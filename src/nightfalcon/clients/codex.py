from __future__ import annotations

from pathlib import Path

from .common import PayloadAdapter


class CodexAdapter(PayloadAdapter):
    name = "codex"
    executable_name = "codex"

    def _instructions(self, destination: Path) -> tuple[str, ...]:
        return (
            f"codex plugin marketplace add {destination}",
            "codex plugin add nightfalcon@nightfalcon-open",
            "$nightfalcon",
        )
