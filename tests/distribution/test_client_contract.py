from __future__ import annotations

from pathlib import Path
import unittest

from nightfalcon.clients.base import ClientAdapter, Detection, InstallPlan


class IncompleteAdapter(ClientAdapter):
    name = "incomplete"


class ClientContractTests(unittest.TestCase):
    def test_incomplete_adapter_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            IncompleteAdapter()

    def test_detection_and_plan_are_immutable_values(self) -> None:
        detection = Detection(
            client="codex", available=True, executable=Path("/usr/bin/codex"), detail="found"
        )
        plan = InstallPlan(
            client="codex",
            source=Path("/payload/codex"),
            destination=Path("/data/codex/3.0.0"),
            conflicts=(),
            instructions=("codex plugin list",),
        )

        with self.assertRaises(AttributeError):
            detection.available = False  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            plan.client = "claude"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
