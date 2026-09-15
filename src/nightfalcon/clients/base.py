from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..receipts import InstallReceipt


class ClientInstallError(RuntimeError):
    """Client payload cannot be installed without violating ownership."""


@dataclass(frozen=True)
class Detection:
    client: str
    available: bool
    executable: Path | None
    detail: str


@dataclass(frozen=True)
class InstallPlan:
    client: str
    source: Path
    destination: Path
    conflicts: tuple[str, ...]
    instructions: tuple[str, ...]


class ClientAdapter(ABC):
    name: str

    @abstractmethod
    def detect(self) -> Detection:
        raise NotImplementedError

    @abstractmethod
    def plan(self, source: Path, destination: Path) -> InstallPlan:
        raise NotImplementedError

    @abstractmethod
    def install(self, plan: InstallPlan, *, force: bool = False) -> InstallReceipt:
        raise NotImplementedError

    @abstractmethod
    def verify(self, receipt: InstallReceipt) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def uninstall(self, receipt: InstallReceipt) -> list[Path]:
        raise NotImplementedError
