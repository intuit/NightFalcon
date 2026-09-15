from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from collections.abc import Iterator
import zipfile


class EmbeddedPayloadError(RuntimeError):
    """Embedded payload is missing, malformed, or hash-mismatched."""


def _manifest_from_archive(archive: zipfile.ZipFile) -> dict[str, object]:
    try:
        raw = archive.read("nightfalcon/_payload-manifest.json")
        manifest = json.loads(raw)
    except (KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise EmbeddedPayloadError("embedded payload manifest is unreadable") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "files",
        "manifest_sha256",
    }:
        raise EmbeddedPayloadError("embedded payload manifest schema is invalid")
    body = {"schema_version": manifest["schema_version"], "files": manifest["files"]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != manifest["manifest_sha256"]:
        raise EmbeddedPayloadError("embedded payload manifest digest mismatch")
    return manifest


def _extract_embedded(client: str, archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _manifest_from_archive(archive)
        files = manifest["files"]
        if not isinstance(files, list):
            raise EmbeddedPayloadError("embedded payload file list is invalid")
        selected = [entry for entry in files if isinstance(entry, dict) and entry.get("client") == client]
        if not selected:
            raise EmbeddedPayloadError(f"embedded {client} payload is missing")
        folded: set[str] = set()
        for entry in selected:
            if set(entry) != {"path", "client", "size", "sha256", "mode"}:
                raise EmbeddedPayloadError("embedded payload entry schema is invalid")
            raw_path = entry["path"]
            if not isinstance(raw_path, str):
                raise EmbeddedPayloadError("embedded payload path is invalid")
            relative = PurePosixPath(raw_path)
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != client:
                raise EmbeddedPayloadError(f"unsafe embedded payload path: {raw_path}")
            client_relative = PurePosixPath(*relative.parts[1:])
            folded_path = client_relative.as_posix().casefold()
            if not client_relative.parts or folded_path in folded:
                raise EmbeddedPayloadError(f"ambiguous embedded payload path: {raw_path}")
            folded.add(folded_path)
            member_name = f"nightfalcon/_payloads/{raw_path}"
            try:
                info = archive.getinfo(member_name)
            except KeyError as error:
                raise EmbeddedPayloadError(f"missing embedded payload member: {raw_path}") from error
            mode = (info.external_attr >> 16) & 0o177777
            if stat.S_ISLNK(mode) or info.is_dir():
                raise EmbeddedPayloadError(f"unsafe embedded payload member: {raw_path}")
            content = archive.read(info)
            if len(content) != entry["size"]:
                raise EmbeddedPayloadError(f"embedded payload size mismatch: {raw_path}")
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise EmbeddedPayloadError(f"embedded payload sha256 mismatch: {raw_path}")
            target = destination.joinpath(*client_relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(int(entry["mode"], 8))
    return destination


@contextmanager
def payload_source(client: str) -> Iterator[Path]:
    package_payload = Path(__file__).resolve().parent / "_payloads" / client
    if package_payload.is_dir():
        yield package_payload
        return
    repository_payload = Path(__file__).resolve().parents[2] / client
    if repository_payload.is_dir():
        yield repository_payload
        return
    archive_path = Path(sys.argv[0]).resolve()
    if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
        raise EmbeddedPayloadError(f"packaged {client} payload is missing")
    with tempfile.TemporaryDirectory(prefix=f"nightfalcon-{client}-") as temporary:
        yield _extract_embedded(client, archive_path, Path(temporary))
