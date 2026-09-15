from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess


CLIENT_ROOTS = ("claude", "codex", "cursor")
_FORBIDDEN_PARTS = frozenset({".git", "__pycache__", "output"})


class PayloadError(RuntimeError):
    """Payload inventory is incomplete, ambiguous, or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, order=True)
class PayloadFile:
    path: str
    client: str
    size: int
    sha256: str
    mode: str


@dataclass(frozen=True)
class PayloadManifest:
    files: tuple[PayloadFile, ...]
    schema_version: int = 1

    @classmethod
    def build(cls, repo_root: Path) -> "PayloadManifest":
        root = repo_root.resolve(strict=True)
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", *CLIENT_ROOTS],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            raw_paths = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
        elif not (root / ".git").exists():
            raw_paths = []
            for client in CLIENT_ROOTS:
                client_root = root / client
                if client_root.is_symlink() or not client_root.is_dir():
                    raise PayloadError(f"client payload root is missing or unsafe: {client}")
                for candidate in client_root.rglob("*"):
                    if candidate.is_symlink():
                        raise PayloadError(
                            f"payload symlink is not allowed: {candidate.relative_to(root).as_posix()}"
                        )
                    if candidate.is_file():
                        raw_paths.append(candidate.relative_to(root).as_posix())
                    elif not candidate.is_dir():
                        raise PayloadError(
                            f"payload entry is not a regular file: {candidate.relative_to(root).as_posix()}"
                        )
        else:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise PayloadError(f"cannot enumerate tracked payload files: {message}")
        entries: list[PayloadFile] = []
        casefolded: set[str] = set()
        for raw in sorted(raw_paths):
            relative = PurePosixPath(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise PayloadError(f"unsafe tracked payload path: {raw}")
            if not relative.parts or relative.parts[0] not in CLIENT_ROOTS:
                raise PayloadError(f"unexpected tracked payload root: {raw}")
            if _FORBIDDEN_PARTS.intersection(relative.parts) or raw.endswith((".pyc", ".DS_Store")):
                raise PayloadError(f"generated file is tracked in payload: {raw}")
            folded = raw.casefold()
            if folded in casefolded:
                raise PayloadError(f"case-colliding payload path: {raw}")
            casefolded.add(folded)
            source = root.joinpath(*relative.parts)
            if source.is_symlink():
                raise PayloadError(f"tracked payload symlink is not allowed: {raw}")
            if not source.is_file():
                raise PayloadError(f"tracked payload is not a regular file: {raw}")
            permissions = stat.S_IMODE(source.stat().st_mode)
            normalized_mode = "0755" if permissions & 0o111 else "0644"
            entries.append(
                PayloadFile(
                    path=raw,
                    client=relative.parts[0],
                    size=source.stat().st_size,
                    sha256=_sha256(source),
                    mode=normalized_mode,
                )
            )
        if {entry.client for entry in entries} != set(CLIENT_ROOTS):
            raise PayloadError("each client payload must contain tracked files")
        return cls(files=tuple(entries))

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "files": [asdict(entry) for entry in self.files],
        }

    @property
    def sha256(self) -> str:
        raw = json.dumps(self._body(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def to_json(self) -> str:
        payload = self._body()
        payload["manifest_sha256"] = self.sha256
        return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"

    def verify(self, root: Path) -> list[str]:
        errors: list[str] = []
        base = root.resolve(strict=True)
        for entry in self.files:
            source = base.joinpath(*PurePosixPath(entry.path).parts)
            if source.is_symlink():
                errors.append(f"symlink: {entry.path}")
                continue
            if not source.is_file():
                errors.append(f"missing: {entry.path}")
                continue
            if source.stat().st_size != entry.size:
                errors.append(f"size mismatch: {entry.path}")
            if _sha256(source) != entry.sha256:
                errors.append(f"sha256 mismatch: {entry.path}")
        return errors
