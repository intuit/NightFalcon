from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any


_CLIENTS = frozenset({"claude", "codex", "cursor"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FIELDS = frozenset(
    {"version", "client", "destination", "manifest_sha256", "paths", "created_at"}
)


class ReceiptError(ValueError):
    """Installation receipt is malformed or unsafe."""


def _validate_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ReceiptError("receipt paths must be non-empty POSIX paths")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReceiptError(f"unsafe receipt path: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise ReceiptError(f"non-canonical receipt path: {raw!r}")
    return normalized


@dataclass(frozen=True)
class InstallReceipt:
    version: str
    client: str
    destination: str
    manifest_sha256: str
    paths: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ReceiptError("invalid receipt version")
        if self.client not in _CLIENTS:
            raise ReceiptError("invalid receipt client")
        if not isinstance(self.destination, str) or not self.destination:
            raise ReceiptError("invalid receipt destination")
        if not _DIGEST.fullmatch(self.manifest_sha256):
            raise ReceiptError("manifest_sha256 must be lowercase SHA-256")
        if not isinstance(self.paths, tuple):
            raise ReceiptError("receipt paths must be a tuple")
        normalized = tuple(_validate_relative_path(item) for item in self.paths)
        if len(set(normalized)) != len(normalized):
            raise ReceiptError("receipt paths must be unique")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ReceiptError("receipt paths must not collide by case")
        if not _CREATED_AT.fullmatch(self.created_at):
            raise ReceiptError("created_at must use UTC second precision")

    def to_json(self) -> str:
        payload = asdict(self)
        payload["paths"] = list(self.paths)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _decode(payload: Any) -> InstallReceipt:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ReceiptError("receipt must contain exact schema fields")
    if not isinstance(payload.get("paths"), list):
        raise ReceiptError("receipt paths must be an array")
    try:
        return InstallReceipt(
            version=payload["version"],
            client=payload["client"],
            destination=payload["destination"],
            manifest_sha256=payload["manifest_sha256"],
            paths=tuple(payload["paths"]),
            created_at=payload["created_at"],
        )
    except (KeyError, TypeError, ReceiptError) as error:
        if isinstance(error, ReceiptError):
            raise
        raise ReceiptError("receipt contains invalid field types") from error


def load_receipt(path: Path) -> InstallReceipt:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"cannot read receipt: {error}") from error
    receipt = _decode(payload)
    if raw != receipt.to_json():
        raise ReceiptError("receipt JSON is not canonical")
    return receipt


def write_receipt(path: Path, receipt: InstallReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(receipt.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
