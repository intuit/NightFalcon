from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from .base import ClientAdapter, ClientInstallError, Detection, InstallPlan
from .. import __version__
from ..filesystem import InstallSafetyError, atomic_activate, safe_remove_receipt_paths, validate_destination
from ..receipts import InstallReceipt, ReceiptError, load_receipt, write_receipt


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    size: int
    sha256: str
    mode: str


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _inventory(root: Path) -> tuple[tuple[_TreeEntry, ...], str]:
    if root.is_symlink() or not root.is_dir():
        raise ClientInstallError("payload root must be a real directory")
    entries: list[_TreeEntry] = []
    folded: set[str] = set()
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise ClientInstallError(f"payload symlink is not allowed: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ClientInstallError(f"payload entry is not a regular file: {relative}")
        if relative.casefold() in folded:
            raise ClientInstallError(f"payload paths collide by case: {relative}")
        folded.add(relative.casefold())
        mode = "0755" if stat.S_IMODE(candidate.stat().st_mode) & 0o111 else "0644"
        entries.append(
            _TreeEntry(
                path=relative,
                size=candidate.stat().st_size,
                sha256=_digest(candidate),
                mode=mode,
            )
        )
    if not entries:
        raise ClientInstallError("payload must contain files")
    raw = json.dumps(
        [entry.__dict__ for entry in entries], sort_keys=True, separators=(",", ":")
    ).encode()
    return tuple(entries), hashlib.sha256(raw).hexdigest()


def _remove_empty_tree(path: Path, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _remove_receipt_directories(destination: Path, paths: tuple[str, ...], stop: Path) -> None:
    directories: set[Path] = {destination}
    for relative in paths:
        current = (destination / relative).parent
        while current != destination.parent:
            directories.add(current)
            current = current.parent
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        if directory == stop:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


class PayloadAdapter(ClientAdapter):
    executable_name: str

    def __init__(
        self,
        *,
        root: Path,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self._which = which
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def detect(self) -> Detection:
        executable = self._which(self.executable_name)
        return Detection(
            client=self.name,
            available=executable is not None,
            executable=Path(executable) if executable else None,
            detail="found" if executable else "not found on PATH",
        )

    def _instructions(self, destination: Path) -> tuple[str, ...]:
        raise NotImplementedError

    def plan(self, source: Path, destination: Path) -> InstallPlan:
        resolved_source = source.resolve(strict=True)
        if source.is_symlink() or not resolved_source.is_dir():
            raise ClientInstallError("payload source must be a real directory")
        if not self.root.exists():
            self.root.mkdir(parents=True, mode=0o700)
        try:
            resolved_destination = validate_destination(destination, self.root)
        except InstallSafetyError as error:
            raise ClientInstallError(str(error)) from error
        conflicts: list[str] = []
        if resolved_destination.exists() or resolved_destination.is_symlink():
            conflicts.append("destination exists")
        receipt_path = self._receipt_path(resolved_destination)
        if receipt_path.exists() and "destination exists" not in conflicts:
            conflicts.append("receipt exists")
        return InstallPlan(
            client=self.name,
            source=resolved_source,
            destination=resolved_destination,
            conflicts=tuple(conflicts),
            instructions=self._instructions(resolved_destination),
        )

    def _receipt_path(self, destination: Path) -> Path:
        return destination.parent / f"{self.name}.receipt.json"

    def _copy_to_stage(self, source: Path, stage: Path, entries: tuple[_TreeEntry, ...]) -> None:
        stage.mkdir(mode=0o700)
        for entry in entries:
            destination = stage.joinpath(*entry.path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / entry.path, destination, follow_symlinks=False)
            destination.chmod(int(entry.mode, 8))

    def _validated_existing_receipt(self, destination: Path) -> InstallReceipt:
        try:
            receipt = load_receipt(self._receipt_path(destination))
        except ReceiptError as error:
            raise ClientInstallError("existing installation has no valid receipt") from error
        if receipt.client != self.name or Path(receipt.destination).resolve() != destination.resolve():
            raise ClientInstallError("existing receipt does not own destination")
        errors = self.verify(receipt)
        if errors:
            raise ClientInstallError("existing installation is not receipt-exact: " + "; ".join(errors))
        return receipt

    def install(self, plan: InstallPlan, *, force: bool = False) -> InstallReceipt:
        if plan.client != self.name:
            raise ClientInstallError("install plan belongs to another client")
        if plan.conflicts and not force:
            raise ClientInstallError("installation conflicts: " + "; ".join(plan.conflicts))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            destination = validate_destination(plan.destination, self.root)
        except InstallSafetyError as error:
            raise ClientInstallError(str(error)) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        entries, manifest_digest = _inventory(plan.source)
        stage = destination.parent / f".{self.name}-stage-{uuid.uuid4().hex}"
        backup = destination.parent / f".{self.name}-backup-{uuid.uuid4().hex}"
        previous: InstallReceipt | None = None
        try:
            self._copy_to_stage(plan.source, stage, entries)
            if destination.exists() or destination.is_symlink():
                if not force:
                    raise ClientInstallError("destination exists")
                previous = self._validated_existing_receipt(destination)
                os.replace(destination, backup)
            atomic_activate(stage, destination)
        except (OSError, InstallSafetyError) as error:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise ClientInstallError(f"installation failed: {error}") from error
        finally:
            if stage.exists():
                _remove_empty_tree(stage, stage.parent)

        created = self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        receipt = InstallReceipt(
            version=__version__,
            client=self.name,
            destination=str(destination),
            manifest_sha256=manifest_digest,
            paths=tuple(entry.path for entry in entries),
            created_at=created,
        )
        try:
            write_receipt(self._receipt_path(destination), receipt)
        except OSError as error:
            try:
                safe_remove_receipt_paths(receipt, self.root)
                _remove_receipt_directories(destination, receipt.paths, self.root)
                if backup.exists():
                    os.replace(backup, destination)
            except (OSError, InstallSafetyError) as rollback_error:
                raise ClientInstallError(
                    f"receipt write failed and rollback was incomplete: {rollback_error}"
                ) from error
            raise ClientInstallError(f"receipt write failed: {error}") from error
        if previous is not None and backup.exists():
            backup_receipt = replace(previous, destination=str(backup))
            safe_remove_receipt_paths(backup_receipt, self.root)
            _remove_receipt_directories(backup, backup_receipt.paths, self.root)
        return receipt

    def verify(self, receipt: InstallReceipt) -> list[str]:
        errors: list[str] = []
        if receipt.client != self.name:
            return ["receipt client mismatch"]
        destination = Path(receipt.destination)
        try:
            validate_destination(destination, self.root)
            entries, digest = _inventory(destination)
        except (ClientInstallError, InstallSafetyError, OSError) as error:
            return [str(error)]
        actual_paths = tuple(entry.path for entry in entries)
        if actual_paths != receipt.paths:
            errors.append("path inventory mismatch")
        if digest != receipt.manifest_sha256:
            errors.append("manifest digest mismatch")
        return errors

    def uninstall(self, receipt: InstallReceipt) -> list[Path]:
        if self.verify(receipt):
            raise ClientInstallError("installed payload does not match receipt")
        destination = Path(receipt.destination).resolve()
        stored = self._validated_existing_receipt(destination)
        if stored != receipt:
            raise ClientInstallError("provided receipt differs from stored receipt")
        removed = safe_remove_receipt_paths(receipt, self.root)
        receipt_path = self._receipt_path(destination)
        receipt_path.unlink()
        _remove_receipt_directories(destination, receipt.paths, self.root.resolve())
        return removed
