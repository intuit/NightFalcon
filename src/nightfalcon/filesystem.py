from __future__ import annotations

import os
from pathlib import Path

from .receipts import InstallReceipt


class InstallSafetyError(RuntimeError):
    """Requested filesystem mutation is outside NightFalcon ownership."""


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InstallSafetyError(f"{label} cannot be resolved: {error}") from error
    if path.is_symlink() or not resolved.is_dir():
        raise InstallSafetyError(f"{label} must be a real directory")
    return resolved


def validate_destination(destination: Path, root: Path) -> Path:
    resolved_root = _resolved_directory(root, "NightFalcon data root")
    try:
        lexical_root = Path(os.path.abspath(root))
        lexical_destination = Path(os.path.abspath(destination))
        try:
            lexical_relative = lexical_destination.relative_to(lexical_root)
            lexical_base = lexical_root
        except ValueError:
            lexical_relative = lexical_destination.relative_to(resolved_root)
            lexical_base = resolved_root
        resolved_destination = destination.resolve(strict=False)
        relative = resolved_destination.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise InstallSafetyError("destination escapes NightFalcon data root") from error
    if not relative.parts or not lexical_relative.parts:
        raise InstallSafetyError("destination must be below NightFalcon data root")

    current = lexical_base
    for part in lexical_relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise InstallSafetyError("destination parent must not be a symlink")
    if lexical_destination.is_symlink():
        raise InstallSafetyError("destination must not be a symlink")
    return resolved_destination


def atomic_activate(staged: Path, destination: Path) -> None:
    if staged.is_symlink() or not staged.is_dir():
        raise InstallSafetyError("staged installation must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise InstallSafetyError("active destination already exists")
    if staged.parent.resolve() != destination.parent.resolve():
        raise InstallSafetyError("staged and active directories must share a parent")
    os.replace(staged, destination)


def safe_remove_receipt_paths(receipt: InstallReceipt, root: Path) -> list[Path]:
    destination = validate_destination(Path(receipt.destination), root)
    removed: list[Path] = []
    for relative in receipt.paths:
        target = destination.joinpath(*relative.split("/"))
        try:
            target.parent.resolve(strict=True).relative_to(destination.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise InstallSafetyError(f"receipt path escapes destination: {relative}") from error
        if target.is_symlink():
            raise InstallSafetyError(f"receipt path became a symlink: {relative}")
        if target.is_file():
            target.unlink()
            removed.append(target)
        elif target.exists():
            raise InstallSafetyError(f"receipt path is not a file: {relative}")
    return removed
