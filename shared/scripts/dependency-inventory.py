#!/usr/bin/env python3
"""Static, host-independent dependency inventory. Never executes package managers."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def add(rows: list[dict], manifest: Path, ecosystem: str, name: str, selector: object,
        group: str, resolution_status: str = "declared") -> None:
    rows.append({
        "manifest": str(manifest), "ecosystem": ecosystem, "name": name,
        "selector": selector, "group": group, "resolution_status": resolution_status,
        "reachability": "unknown", "analysis_note": "Static declaration; reachability requires separate evidence."
    })


def package_json(path: Path, rows: list[dict]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for field, group in (("dependencies","runtime"),("devDependencies","development"),("peerDependencies","peer"),("optionalDependencies","optional")):
        for name, selector in data.get(field, {}).items(): add(rows, path, "npm", name, selector, group)


def composer_json(path: Path, rows: list[dict]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for field, group in (("require", "runtime"), ("require-dev", "development")):
        for name, selector in data.get(field, {}).items():
            if name != "php":
                add(rows, path, "composer", name, selector, group)


def toml_manifest(path: Path, rows: list[dict]) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if path.name == "pyproject.toml":
        project = data.get("project", {})
        for item in project.get("dependencies", []):
            name = re.split(r"[ <>=!~;\[]", item, 1)[0]
            add(rows, path, "python", name, item, "runtime")
        for extra, items in project.get("optional-dependencies", {}).items():
            for item in items:
                name = re.split(r"[ <>=!~;\[]", item, 1)[0]
                add(rows, path, "python", name, item, f"optional:{extra}")
        poetry = data.get("tool", {}).get("poetry", {})
        for name, selector in poetry.get("dependencies", {}).items():
            if name.lower() != "python":
                add(rows, path, "python", name, selector, "runtime")
        for name, selector in poetry.get("dev-dependencies", {}).items():
            add(rows, path, "python", name, selector, "development")
        for group_name, group in poetry.get("group", {}).items():
            dependencies = group.get("dependencies", {}) if isinstance(group, dict) else {}
            category = "development" if group_name.lower() == "dev" else f"poetry:{group_name}"
            for name, selector in dependencies.items():
                add(rows, path, "python", name, selector, category)
    elif path.name == "Cargo.toml":
        for field, group in (("dependencies","runtime"),("dev-dependencies","development"),("build-dependencies","build")):
            for name, selector in data.get(field, {}).items(): add(rows, path, "cargo", name, selector, group)
        for name, selector in data.get("workspace", {}).get("dependencies", {}).items():
            add(rows, path, "cargo", name, selector, "workspace")
        for target, table in data.get("target", {}).items():
            for field in ("dependencies","dev-dependencies","build-dependencies"):
                for name, selector in table.get(field, {}).items(): add(rows, path, "cargo", name, selector, f"target:{target}:{field}")
        for feature, members in data.get("features", {}).items():
            for member in members: add(rows, path, "cargo-feature", member, feature, f"feature:{feature}")


def line_manifest(path: Path, rows: list[dict]) -> None:
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"): continue
        if path.name == "go.mod":
            single = re.match(r"^require\s+(\S+)\s+(\S+)", line)
            direct = re.match(r"^([\w.-]+\.[\w./-]+)\s+(v\S+)", line)
            if single:
                add(rows, path, "go", single.group(1), single.group(2), "runtime")
            elif direct:
                add(rows, path, "go", direct.group(1), direct.group(2), "runtime")
        elif path.name.startswith("requirements"):
            name = re.split(r"[ <>=!~;\[]", line, 1)[0]; add(rows, path, "python", name, line, "requirements")
        elif path.name == "Gemfile" and line.startswith("gem "):
            match = re.match(r"gem\s+['\"]([^'\"]+)['\"](.*)", line)
            if match: add(rows, path, "rubygems", match.group(1), match.group(2).strip(" ,"), "runtime")


def read_package_lock(path: Path, rows: list[dict]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for name, entry in data.get("packages", {}).items():
        if not name or not isinstance(entry, dict):
            continue
        add(rows, path, "npm", name.removeprefix("node_modules/"), entry.get("version", "unknown"), "transitive", "resolved")


def json_lock(path: Path, rows: list[dict]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if path.name == "composer.lock":
        for field, group in (("packages", "runtime"), ("packages-dev", "development")):
            for entry in data.get(field, []):
                if isinstance(entry, dict) and entry.get("name"):
                    add(rows, path, "composer", entry["name"], entry.get("version", "unknown"), group, "resolved")
    elif path.name == "Pipfile.lock":
        for field, group in (("default", "runtime"), ("develop", "development")):
            for name, entry in data.get(field, {}).items():
                selector = entry.get("version", "unknown") if isinstance(entry, dict) else entry
                add(rows, path, "python", name, selector, group, "resolved")
    elif path.name == "packages.lock.json":
        for framework, dependencies in data.get("dependencies", {}).items():
            if not isinstance(dependencies, dict):
                continue
            for name, entry in dependencies.items():
                selector = entry.get("resolved", entry.get("requested", "unknown")) if isinstance(entry, dict) else entry
                add(rows, path, "nuget", name, selector, f"framework:{framework}", "resolved")


def toml_lock(path: Path, rows: list[dict]) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    ecosystem = "cargo" if path.name == "Cargo.lock" else "python"
    for entry in data.get("package", []):
        if isinstance(entry, dict) and entry.get("name"):
            add(rows, path, ecosystem, entry["name"], entry.get("version", "unknown"), "transitive", "resolved")


def yarn_lock(path: Path, rows: list[dict]) -> None:
    current: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
            key = raw.rstrip()[:-1].strip('"')
            current = [part.strip().strip('"') for part in key.split(",")]
        elif current and re.match(r"^\s+version\s+", raw):
            version = raw.strip().split(None, 1)[1].strip('"')
            for selector in current:
                marker = selector.rfind("@")
                name = selector[:marker] if marker > 0 else selector
                if name:
                    add(rows, path, "npm", name, version, "transitive", "resolved")
            current = []


def pnpm_lock(path: Path, rows: list[dict]) -> None:
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"^\s{2,}['\"]?/?(@?[^\s:'\"]+(?:/[^\s:'\"]+)?)@([^\s:'\"]+)['\"]?:\s*$", raw)
        if match:
            add(rows, path, "npm", match.group(1), match.group(2), "transitive", "resolved")


def line_lock(path: Path, rows: list[dict]) -> None:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if path.name == "go.sum":
        seen = set()
        for raw in lines:
            parts = raw.split()
            if len(parts) >= 2:
                key = (parts[0], parts[1].removesuffix("/go.mod"))
                if key not in seen:
                    add(rows, path, "go", key[0], key[1], "transitive", "resolved")
                    seen.add(key)
    elif path.name == "gradle.lockfile":
        for raw in lines:
            match = re.match(r"^([^:#=]+):([^:#=]+):([^=\s]+)=", raw.strip())
            if match:
                add(rows, path, "gradle", f"{match.group(1)}:{match.group(2)}", match.group(3), "transitive", "resolved")
    elif path.name == "Gemfile.lock":
        in_specs = False
        for raw in lines:
            if raw == "  specs:":
                in_specs = True
                continue
            if in_specs and raw and not raw.startswith("    "):
                in_specs = False
            if in_specs:
                match = re.match(r"^    ([^\s(]+) \(([^)]+)\)", raw)
                if match:
                    add(rows, path, "rubygems", match.group(1), match.group(2), "transitive", "resolved")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(node: ET.Element, name: str, default: str = "unknown") -> str:
    for child in node:
        if local_name(child.tag) == name:
            return (child.text or "").strip() or default
    return default


def maven_manifest(path: Path, rows: list[dict]) -> None:
    root = ET.parse(path).getroot()

    def dependencies(container: ET.Element, group_prefix: str) -> None:
        for child in container:
            if local_name(child.tag) != "dependencies":
                continue
            for dep in child:
                if local_name(dep.tag) != "dependency":
                    continue
                group_id = child_text(dep, "groupId")
                artifact_id = child_text(dep, "artifactId")
                scope = child_text(dep, "scope", "runtime")
                optional = child_text(dep, "optional", "false") == "true"
                group = f"{group_prefix}:{scope}" if group_prefix else scope
                if optional:
                    group += ":optional"
                add(rows, path, "maven", f"{group_id}:{artifact_id}", child_text(dep, "version"), group)

    dependencies(root, "")
    for child in root:
        if local_name(child.tag) == "dependencyManagement":
            dependencies(child, "dependency-management")
        elif local_name(child.tag) == "profiles":
            for profile in child:
                if local_name(profile.tag) == "profile":
                    dependencies(profile, f"profile:{child_text(profile, 'id')}")


def nuget_manifest(path: Path, rows: list[dict]) -> None:
    root = ET.parse(path).getroot()
    for group in root.iter():
        if local_name(group.tag) != "ItemGroup":
            continue
        condition = group.attrib.get("Condition", "").strip()
        group_name = f"condition:{condition}" if condition else "runtime"
        for reference in group:
            if local_name(reference.tag) not in {"PackageReference", "PackageVersion"}:
                continue
            name = reference.attrib.get("Include") or reference.attrib.get("Update")
            if not name:
                continue
            selector = reference.attrib.get("Version") or child_text(reference, "Version")
            add(rows, path, "nuget", name, selector, group_name)


def gradle_manifest(path: Path, rows: list[dict]) -> None:
    dependency = re.compile(
        r"^\s*([A-Za-z][\w-]*)\s*(?:\(|\s)\s*['\"]([^:'\"]+):([^:'\"]+):([^'\"]+)['\"]"
    )
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = dependency.search(line)
        if match:
            configuration, group_id, artifact, selector = match.groups()
            add(rows, path, "gradle", f"{group_id}:{artifact}", selector, configuration)


def inventory(root: Path) -> dict:
    rows: list[dict] = []
    coverage: list[dict] = []
    names = {
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "composer.json", "composer.lock", "pyproject.toml", "poetry.lock",
        "Pipfile.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
        "Gemfile", "Gemfile.lock", "pom.xml", "packages.lock.json",
        "gradle.lockfile",
    }
    for path in sorted(root.rglob("*")):
        if any(part in {".git", "node_modules", "vendor"} for part in path.parts):
            continue
        supported = path.name in names or path.name.startswith("requirements") or path.suffix in {".csproj", ".fsproj", ".vbproj"} or path.name in {"Directory.Packages.props", "build.gradle", "build.gradle.kts"}
        if not supported:
            continue
        relative = str(path.relative_to(root))
        if path.is_symlink():
            coverage.append({"manifest": relative, "status": "rejected", "reason": "symbolic links are not read"})
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            coverage.append({"manifest": relative, "status": "rejected", "reason": "path escapes inventory root or is unreadable"})
            continue
        if not resolved.is_file():
            continue
        before = len(rows)
        try:
            if path.name == "package.json": package_json(path, rows)
            elif path.name == "package-lock.json": read_package_lock(path, rows)
            elif path.name == "yarn.lock": yarn_lock(path, rows)
            elif path.name == "pnpm-lock.yaml": pnpm_lock(path, rows)
            elif path.name == "composer.json": composer_json(path, rows)
            elif path.name in {"composer.lock", "Pipfile.lock", "packages.lock.json"}: json_lock(path, rows)
            elif path.name in {"Cargo.lock", "poetry.lock"}: toml_lock(path, rows)
            elif path.name in {"go.sum", "Gemfile.lock", "gradle.lockfile"}: line_lock(path, rows)
            elif path.name == "pom.xml": maven_manifest(path, rows)
            elif path.suffix in {".csproj", ".fsproj", ".vbproj"} or path.name == "Directory.Packages.props": nuget_manifest(path, rows)
            elif path.name in {"build.gradle", "build.gradle.kts"}: gradle_manifest(path, rows)
            elif path.name in {"pyproject.toml", "Cargo.toml"}: toml_manifest(path, rows)
            elif path.name in names or path.name.startswith("requirements"): line_manifest(path, rows)
        except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError, ET.ParseError) as exc:
            rows.append({"manifest":str(path.relative_to(root)),"parse_error":str(exc),"resolution_status":"unknown","reachability":"unknown"})
            coverage.append({"manifest": relative, "status": "error", "reason": type(exc).__name__})
        else:
            coverage.append({"manifest": relative, "status": "parsed", "records": len(rows) - before})
    for row in rows:
        if "manifest" in row:
            try: row["manifest"] = str(Path(row["manifest"]).relative_to(root))
            except ValueError: pass
    return {
        "schema_version":"1", "dependency_inventory":rows,
        "manifest_coverage":coverage, "manifests_detected":len(coverage),
        "package_manager_execution":False, "network_access":False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("root", type=Path); parser.add_argument("--output", type=Path)
    args = parser.parse_args(); root = args.root.resolve()
    if not root.is_dir(): parser.error(f"root is not a directory: {root}")
    data = inventory(root); text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else: print(text, end="")
    return 0


if __name__ == "__main__": raise SystemExit(main())
