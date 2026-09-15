from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def finalize_release_directory(
    directory: Path, *, version: str, payload_manifest_sha256: str
) -> dict[str, object]:
    artifacts = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name == "release-manifest.json":
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"release output must contain only regular files: {path.name}")
        content = path.read_bytes()
        artifacts.append(
            {
                "name": path.name,
                "size": len(content),
                "sha256": sha256_bytes(content),
            }
        )
    if not artifacts:
        raise RuntimeError("release output contains no artifacts")
    manifest: dict[str, object] = {
        "schema_version": 2,
        "version": version,
        "python_requires": ">=3.11",
        "payload_manifest_sha256": payload_manifest_sha256,
        "artifacts": artifacts,
    }
    (directory / "release-manifest.json").write_text(
        canonical_json(manifest), encoding="utf-8", newline="\n"
    )
    return manifest


def spdx_document(version: str, members: Iterable[tuple[str, bytes]]) -> dict[str, object]:
    files = []
    for index, (name, content) in enumerate(members, start=1):
        files.append(
            {
                "SPDXID": f"SPDXRef-File-{index}",
                "fileName": f"./{name}",
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": sha256_bytes(content)}
                ],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"nightfalcon-{version}",
        "documentNamespace": f"https://github.com/intuit/nightfalcon/releases/{version}/spdx",
        "creationInfo": {
            "created": "1980-01-01T00:00:00Z",
            "creators": ["Tool: NightFalcon deterministic release builder"],
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-Package-NightFalcon",
                "name": "nightfalcon",
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": "Apache-2.0",
                "licenseDeclared": "Apache-2.0",
                "copyrightText": "NOASSERTION",
            }
        ],
        "files": files,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-Package-NightFalcon",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": item["SPDXID"],
            }
            for item in files
        ],
    }
