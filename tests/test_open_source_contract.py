import json
import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTS = (ROOT / "claude", ROOT / "codex", ROOT / "cursor")
SOURCE_SHA = "0c3183e02ec1f1500115233402cabdf7808ec4e6"
GENERATED_DIRECTORY_EXCLUSIONS = {"__pycache__", ".terragraph"}


class OpenSourceContractTests(unittest.TestCase):
    def test_three_ports_and_migration_manifest_exist(self):
        for port in PORTS:
            self.assertTrue(port.is_dir(), port)
        manifest = json.loads((ROOT / "migration-manifest.json").read_text())
        self.assertEqual(manifest["source_commit"], SOURCE_SHA)
        self.assertEqual(set(manifest["ports"]), {"claude", "codex", "cursor"})
        self.assertEqual(manifest["schema_version"], "2")
        self.assertGreater(manifest["release_file_count"], 0)
        self.assertRegex(manifest["release_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["excluded_commitment_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("paths", manifest["exclusions"])
        commitments = manifest["exclusions"]["category_commitments"]
        self.assertEqual(set(commitments), {"private_context", "internal_work_products", "generated_evidence"})
        self.assertEqual(commitments["private_context"]["file_count"], 3837)
        for value in commitments.values():
            self.assertGreater(value["file_count"], 0)
            self.assertRegex(value["sha256"], r"^[0-9a-f]{64}$")

    def test_three_port_source_migration_map_is_complete_and_hash_bound(self):
        payload = json.loads((ROOT / "source-migration-map.json").read_text())
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["source_revision"], SOURCE_SHA)
        self.assertEqual(set(payload["ports"]), {"claude", "codex", "cursor"})
        classifications = set()
        for name, destination_root in zip(("claude", "codex", "cursor"), PORTS):
            port = payload["ports"][name]
            self.assertEqual(
                port["source_file_count"],
                port["included_source_file_count"] + port["excluded_source_file_count"]
                + len(port.get("post_migration_exclusions", [])),
            )
            self.assertGreater(port["excluded_source_file_count"], 0)
            self.assertRegex(port["excluded_source_sha256"], r"^[0-9a-f]{64}$")
            expected_destinations = {
                path.relative_to(destination_root).as_posix()
                for path in destination_root.rglob("*")
                if path.is_file()
                and path.relative_to(destination_root).as_posix() not in set(port["destination_scope_exclusions"])
                and not GENERATED_DIRECTORY_EXCLUSIONS.intersection(path.parts)
                and path.suffix != ".pyc"
            }
            expected_exclusions = (
                {"verification-results.json", "docs/verification-report.md"}
                if name == "codex" else set()
            )
            self.assertEqual(set(port["destination_scope_exclusions"]), expected_exclusions)
            entries = port["files"]
            self.assertEqual(port["destination_file_count"], len(expected_destinations))
            self.assertEqual({item["destination"] for item in entries}, expected_destinations)
            mapped_sources = []
            for item in entries:
                classifications.add(item["classification"])
                path = destination_root / item["destination"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["destination_sha256"])
                if item["classification"] == "PUBLIC_ADDITION":
                    self.assertIsNone(item["source"])
                    self.assertIsNone(item["source_sha256"])
                else:
                    mapped_sources.append(item["source"])
                    if item["classification"] == "UNCHANGED":
                        self.assertEqual(item["source_sha256"], item["destination_sha256"])
                    else:
                        self.assertNotEqual(item["source_sha256"], item["destination_sha256"])
            self.assertEqual(len(mapped_sources), len(set(mapped_sources)))
            self.assertEqual(port["included_source_file_count"], len(mapped_sources))
        self.assertEqual(classifications, {"UNCHANGED", "ADAPTED", "PUBLIC_ADDITION"})

    def test_frozen_three_port_source_map_validates(self):
        completed = subprocess.run(
            ["python3", str(ROOT / "scripts/build-source-migration-map.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_migration_manifest_hashes_bind_complete_release_scope(self):
        manifest = json.loads((ROOT / "migration-manifest.json").read_text())
        aggregate = hashlib.sha256()
        count = 0
        port_hashers = {name: hashlib.sha256() for name in ("claude", "codex", "cursor")}
        excluded = set(manifest["release_scope_exclusions"])
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative_text = path.relative_to(ROOT).as_posix()
            if (
                relative_text in excluded
                or GENERATED_DIRECTORY_EXCLUSIONS.intersection(path.parts)
                or path.suffix == ".pyc"
            ):
                continue
            record = relative_text.encode() + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n"
            aggregate.update(record)
            top_level = relative_text.split("/", 1)[0]
            if top_level in port_hashers:
                port_hashers[top_level].update(record)
            count += 1
        self.assertEqual(manifest["release_file_count"], count)
        self.assertEqual(manifest["release_sha256"], aggregate.hexdigest())
        self.assertEqual(manifest["port_sha256"], {name: value.hexdigest() for name, value in port_hashers.items()})

    def test_noncommercial_owasp_projects_are_reference_only(self):
        provenance = json.loads((ROOT / "owasp-provenance.json").read_text())
        by_id = {item["id"]: item for item in provenance["frameworks"]}
        for project in ("kubernetes", "mcp", "smart-contract"):
            self.assertEqual(by_id[project]["license"], "CC-BY-NC-SA-4.0")
            self.assertEqual(by_id[project]["usage"], "identifier-and-title-reference-only")
            self.assertFalse(by_id[project]["bundled_adaptation"])
        mcp = (ROOT / "shared/owasp/OWASP_Cloud_Agentic_and_Identity.md").read_text()
        self.assertIn("External reference only", mcp)
        self.assertNotIn("MCP01 Token", mcp)

    def test_package_has_no_private_identifiers(self):
        former_org = "in" + "tuit"
        canonical_public_repository = (
            "https://github.com/" + former_org + "/nightfalcon"
        )
        forbidden = re.compile(
            rf"(?i)({former_org}|github\.{former_org}\.com|quick" +
            r"books|turbo" + r"tax|credit[ -]?karma|mail" +
            r"chimp|\bit" + r"ad(?:s)?\b|\bid" + r"ps\b)"
        )
        violations = []
        for port in PORTS:
            for path in port.rglob("*"):
                if not path.is_file() or GENERATED_DIRECTORY_EXCLUSIONS.intersection(path.parts):
                    continue
                try:
                    text = path.read_text(errors="strict")
                except (UnicodeDecodeError, OSError):
                    continue
                public_metadata_removed = text.replace(
                    canonical_public_repository, ""
                )
                if forbidden.search(public_metadata_removed):
                    violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(violations, [])

    def test_packaged_shell_entry_points_parse(self):
        scripts = (
            ROOT / "claude/scripts/check-gate.sh",
            ROOT / "codex/plugins/nightfalcon/scripts/check-gate.sh",
            ROOT / "cursor/scripts/check-gate.sh",
        )
        for script in scripts:
            completed = subprocess.run(
                ["bash", "-n", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(script=script.relative_to(ROOT)):
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_documented_client_invocations_match_live_client_discovery(self):
        root_readme = (ROOT / "README.md").read_text()
        claude_readme = (ROOT / "claude/README.md").read_text()
        claude_agents = (ROOT / "claude/AGENTS.md").read_text()
        claude_instructions = (ROOT / "claude/CLAUDE.md").read_text()
        claude_command = (ROOT / "claude/commands/nightfalcon.md").read_text()
        cursor_readme = (ROOT / "cursor/README.md").read_text()
        codex_readme = (ROOT / "codex/README.md").read_text()

        self.assertIn("| Claude Code | [`claude/`](claude/) | `/nightfalcon:nightfalcon` |", root_readme)
        self.assertIn("| Codex | [`codex/`](codex/) | `$nightfalcon` |", root_readme)
        self.assertIn("| Cursor | [`cursor/`](cursor/) | `/nightfalcon` |", root_readme)

        for path, text in (
            ("claude/README.md", claude_readme),
            ("claude/AGENTS.md", claude_agents),
            ("claude/CLAUDE.md", claude_instructions),
            ("claude/commands/nightfalcon.md", claude_command),
        ):
            self.assertIn("/nightfalcon:nightfalcon", text, path)
            self.assertIsNone(
                re.search(r"(?<!:)`/nightfalcon(?:[ `]|$)", text),
                f"bare Claude plugin invocation remains in {path}",
            )
        self.assertNotIn("do **not** re-run `init-review.sh`", claude_command)
        self.assertNotIn('--date "$(date +%Y-%m-%d)"', claude_command)
        self.assertIn("canonical `state.json.date`", claude_command)
        for name, command in (
            ("claude", claude_command),
            ("cursor", (ROOT / "cursor/.cursor/commands/nightfalcon.md").read_text()),
        ):
            self.assertIn("canonical `state.json`", command, name)
            self.assertIn("without asking again", command, name)

        self.assertRegex(cursor_readme, r"open\s+`cursor/` itself as the Cursor workspace")
        self.assertNotIn("clone this repo and work from\n   its root", cursor_readme)
        self.assertNotIn("--disable code_mode_host", codex_readme)
        self.assertIn("Do not\ndisable `code_mode_host`", codex_readme)
        self.assertIn("codex-code-mode-host", codex_readme)

    def test_claude_install_selector_and_runtime_variables_match_manifests(self):
        marketplace = json.loads((ROOT / "claude/.claude-plugin/marketplace.json").read_text())
        plugin = json.loads((ROOT / "claude/.claude-plugin/plugin.json").read_text())
        readme = (ROOT / "claude/README.md").read_text()
        skill = (ROOT / "claude/skills/nightfalcon/SKILL.md").read_text()

        selector = f"{plugin['name']}@{marketplace['name']}"
        self.assertIn(f"/plugin install {selector}", readme)
        self.assertIn(f"/plugin update {selector}", readme)
        self.assertIn(f"/plugin uninstall {selector}", readme)
        self.assertNotIn("adversarial-security-review-local", readme)
        self.assertNotIn("CLAUDE_PLUGIN_DIR", skill)
        self.assertIn("$CLAUDE_PLUGIN_ROOT/scripts/agent-journal.py", skill)

    def test_client_hooks_activate_from_durable_workspace_marker(self):
        claude_hooks = (ROOT / "claude/hooks/hooks.json").read_text()
        claude_guard = (ROOT / "claude/hooks/bash-guard.py").read_text()
        claude_skill = (ROOT / "claude/skills/nightfalcon/SKILL.md").read_text()
        cursor_skill = (ROOT / "cursor/.cursor/skills/nightfalcon/SKILL.md").read_text()
        cursor_hook_docs = (ROOT / "cursor/references/hook-setup.md").read_text()
        cursor_surfaces = {
            "command": (ROOT / "cursor/.cursor/commands/nightfalcon.md").read_text(),
            "rule": (ROOT / "cursor/.cursor/rules/nightfalcon.mdc").read_text(),
            "skill": cursor_skill,
            "readme": (ROOT / "cursor/README.md").read_text(),
        }

        self.assertNotIn("SECURITY_REVIEW_WORKSPACE", claude_hooks)
        self.assertIn("hooks/workspace.py", claude_hooks)
        self.assertIn("resolve_workspace(payload)", claude_guard)
        claude_resolver = (ROOT / "claude/hooks/workspace.py").read_text()
        self.assertIn('path / "state.json"', claude_resolver)
        self.assertIn('path / ".nightfalcon-review"', claude_resolver)
        self.assertNotIn("export SECURITY_REVIEW_WORKSPACE", claude_skill)
        for name, surface in cursor_surfaces.items():
            self.assertNotIn("export SECURITY_REVIEW_WORKSPACE", surface, name)
            self.assertNotIn("NIGHTFALCON_CURSOR_ACTIVE", surface, name)
        self.assertNotIn("$PLUGIN_ROOT", cursor_skill)
        self.assertIn("state.json", cursor_hook_docs)
        self.assertIn("durable", cursor_hook_docs.lower())

        codex_runtime = "\n".join(
            [
                (ROOT / "codex/plugins/nightfalcon/skills/nightfalcon/SKILL.md").read_text(),
                *((path.read_text() for path in (ROOT / "codex/plugins/nightfalcon/phases").glob("*.md"))),
            ]
        )
        self.assertNotIn("SECURITY_REVIEW_WORKSPACE", codex_runtime)

    def test_claude_model_documentation_matches_advisory_policy(self):
        readme = (ROOT / "claude/README.md").read_text()
        model_section = readme[readme.index("### Model:"):readme.index("### Triage")]
        self.assertIn("advisory", model_section)
        self.assertIn("currently selected", model_section)
        self.assertNotIn("immutable", model_section)
        self.assertNotIn("runs on a specific model", model_section)

    def test_claude_write_hook_uses_cwd_marker_without_workspace_export(self):
        hooks = json.loads((ROOT / "claude/hooks/hooks.json").read_text())
        write_hook = next(
            entry["hooks"][0]["command"]
            for entry in hooks["hooks"]["PreToolUse"]
            if entry["matcher"] == "Write|Edit"
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            initialized = subprocess.run(
                [
                    "bash",
                    str(ROOT / "claude/scripts/init-review.sh"),
                    "--workspace",
                    str(workspace),
                    "--date",
                    "2026-08-27",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment = dict(os.environ)
            environment.pop("SECURITY_REVIEW_WORKSPACE", None)
            environment["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude")
            completed = subprocess.run(
                ["bash", "-c", write_hook],
                input=json.dumps(
                    {
                        "cwd": str(workspace),
                        "tool_input": {"file_path": str(workspace / "state.json")},
                    }
                ),
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("state.json", completed.stderr)

    def test_all_claude_hooks_resolve_marked_workspace_from_nested_cwd(self):
        hooks = json.loads((ROOT / "claude/hooks/hooks.json").read_text())["hooks"]
        commands = {
            "stop": hooks["Stop"][0]["hooks"][0]["command"],
            "write": next(
                entry["hooks"][0]["command"]
                for entry in hooks["PreToolUse"]
                if entry["matcher"] == "Write|Edit"
            ),
            "agent": next(
                entry["hooks"][0]["command"]
                for entry in hooks["PreToolUse"]
                if entry["matcher"] == "Task|Agent"
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            initialized = subprocess.run(
                [
                    "bash", str(ROOT / "claude/scripts/init-review.sh"),
                    "--workspace", str(workspace), "--date", "2026-08-27",
                    "--slug", "demo",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            nested = workspace / "sourcecode/demo/nested"
            nested.mkdir(parents=True)
            environment = dict(os.environ)
            environment.pop("SECURITY_REVIEW_WORKSPACE", None)
            environment.pop("CLAUDE_PROJECT_DIR", None)
            environment["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude")

            def invoke(command, payload):
                return subprocess.run(
                    ["bash", "-c", command], input=json.dumps(payload),
                    capture_output=True, text=True, env=environment, check=False,
                )

            write = invoke(commands["write"], {
                "cwd": str(nested),
                "tool_input": {"file_path": str(workspace / "state.json")},
            })
            stop = invoke(commands["stop"], {
                "cwd": str(nested), "session_id": "nested-session",
            })
            agent = invoke(commands["agent"], {
                "cwd": str(nested), "session_id": "nested-session",
            })
            bash_guard = subprocess.run(
                ["python3", str(ROOT / "claude/hooks/bash-guard.py")],
                input=json.dumps({
                    "tool_name": "Bash", "cwd": str(nested),
                    "tool_input": {"command": 'sed -i "s/phase-0/done/" ../../../state.json'},
                }),
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertEqual(write.returncode, 2, write.stderr)
            self.assertEqual(stop.returncode, 2, stop.stderr)
            self.assertEqual(agent.returncode, 0, agent.stderr)
            self.assertEqual(bash_guard.returncode, 2, bash_guard.stderr)
            self.assertTrue(any((workspace / ".agent-spawns").iterdir()))

    def test_unrelated_state_json_does_not_activate_client_hooks(self):
        claude_hook = ROOT / "claude/hooks/bash-guard.py"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "state.json").write_text('{"current_phase":"phase-3"}\n')
            environment = dict(os.environ)
            environment.pop("SECURITY_REVIEW_WORKSPACE", None)
            environment["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude")
            completed = subprocess.run(
                ["python3", str(claude_hook)],
                input=json.dumps(
                    {
                        "tool_name": "Bash",
                        "cwd": str(workspace),
                        "tool_input": {"command": 'sed -i "s/3/8/" state.json'},
                    }
                ),
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_phase8_has_one_model_policy_and_uses_verified_builder_roots(self):
        codex_phase = (
            ROOT / "codex/plugins/nightfalcon/phases/phase-8.md"
        ).read_text()
        cursor_phase = (ROOT / "cursor/phases/phase-8.md").read_text()
        self.assertEqual(codex_phase.count("**User-selected model.**"), 1)
        self.assertNotIn('$PWD/scripts/report/', cursor_phase)
        for builder in ("build-executive.py", "build.py", "build-sarif.py"):
            self.assertIn(f'$PLUGIN_ROOT/scripts/report/{builder}', cursor_phase)

    def test_initializers_seed_multiple_review_slugs_without_direct_state_edit(self):
        ports = {
            "claude": ROOT / "claude",
            "codex": ROOT / "codex/plugins/nightfalcon",
            "cursor": ROOT / "cursor",
        }
        for name, port in ports.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                completed = subprocess.run(
                    [
                        "bash",
                        str(port / "scripts/init-review.sh"),
                        "--workspace",
                        str(workspace),
                        "--date",
                        "2026-08-27",
                        "--slug",
                        "owner-repo-a",
                        "--slug",
                        "owner-repo-b",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                state = json.loads((workspace / "state.json").read_text())
                self.assertEqual(state["repo_slugs"], ["owner-repo-a", "owner-repo-b"])
                self.assertEqual((workspace / ".nightfalcon-review").read_text(), "nightfalcon\n")
                skill_path = (
                    port / "skills/nightfalcon/SKILL.md"
                    if name != "cursor"
                    else port / ".cursor/skills/nightfalcon/SKILL.md"
                )
                skill = skill_path.read_text()
                self.assertNotIn("s['repo_slugs'] =", skill)
                self.assertIn('INIT_ARGS+=(--slug', skill)

    def test_all_clients_resume_across_calendar_days_without_date_drift(self):
        ports = {
            "claude": ROOT / "claude",
            "codex": ROOT / "codex/plugins/nightfalcon",
            "cursor": ROOT / "cursor",
        }
        for name, port in ports.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                command = [
                    "bash", str(port / "scripts/init-review.sh"),
                    "--workspace", str(workspace), "--date", "2026-08-26",
                    "--slug", "owner-repo",
                ]
                first = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                before = (workspace / "state.json").read_bytes()
                command[command.index("2026-08-26")] = "2026-08-27"
                resumed = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual((workspace / "state.json").read_bytes(), before)
                self.assertEqual(json.loads(before)["date"], "2026-08-26")

    def test_all_clients_reject_repository_scope_change_before_journal_mutation(self):
        ports = {
            "claude": ROOT / "claude",
            "codex": ROOT / "codex/plugins/nightfalcon",
            "cursor": ROOT / "cursor",
        }
        for name, port in ports.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                base = [
                    "bash", str(port / "scripts/init-review.sh"),
                    "--workspace", str(workspace), "--date", "2026-08-27",
                ]
                first = subprocess.run(
                    [*base, "--slug", "owner-repo-a"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                state_before = (workspace / "state.json").read_bytes()
                journal = workspace / "output/agent-conversation-2026-08-27.md"
                journal_before = journal.read_bytes()
                mismatch = subprocess.run(
                    [*base, "--slug", "owner-repo-b"],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(mismatch.returncode, 0)
                self.assertIn("scope differs", mismatch.stderr)
                self.assertEqual((workspace / "state.json").read_bytes(), state_before)
                self.assertEqual(journal.read_bytes(), journal_before)

    def test_current_owasp_provenance(self):
        provenance = json.loads((ROOT / "owasp-provenance.json").read_text())
        frameworks = {item["id"]: item for item in provenance["frameworks"]}
        expected = {
            "web": "2025",
            "api": "2023",
            "mobile": "2024",
            "llm": "2026",
            "agentic": "2026",
            "kubernetes": "2025",
            "mcp": "2025-beta",
            "nhi": "2025",
            "smart-contract": "2026",
            "business-logic-abuse": "2025",
            "asvs": "5.0.0",
        }
        for framework_id, edition in expected.items():
            self.assertEqual(frameworks[framework_id]["edition"], edition)
            self.assertTrue(frameworks[framework_id]["source_url"].startswith("https://"))
            self.assertIn("license", frameworks[framework_id])
        self.assertEqual(frameworks["agentic-skills"]["status"], "public-review-draft")
        self.assertFalse(frameworks["agentic-skills"]["stable"])
        self.assertEqual(frameworks["smart-contract"]["license"], "CC-BY-NC-SA-4.0")
        self.assertEqual(frameworks["smart-contract"]["usage"], "identifier-and-title-reference-only")
        self.assertFalse(frameworks["smart-contract"]["bundled_adaptation"])
        self.assertFalse((ROOT / "shared/owasp/OWASP_Smart_Contract_Top10_2026.md").exists())
        self.assertEqual(frameworks["kubernetes"]["license"], "CC-BY-NC-SA-4.0")
        self.assertEqual(frameworks["kubernetes"]["usage"], "identifier-and-title-reference-only")
        self.assertFalse(frameworks["kubernetes"]["bundled_adaptation"])
        self.assertNotIn("Kubernetes Top 10", (ROOT / "shared/owasp/OWASP_Cloud_Agentic_and_Identity.md").read_text())

    def test_shared_owasp_and_deep_contracts_are_identical_in_each_port(self):
        mappings = {
            "claude": ROOT / "claude",
            "codex": ROOT / "codex/plugins/nightfalcon",
            "cursor": ROOT / "cursor",
        }
        for shared_path in (ROOT / "shared/owasp").iterdir():
            if not shared_path.is_file():
                continue
            for name, port in mappings.items():
                packaged = port / "references/OWASP" / shared_path.name
                self.assertEqual(packaged.read_bytes(), shared_path.read_bytes(), f"{name}:{shared_path.name}")
        for name in (
            "analysis-contracts.md",
            "authorization-relationship.schema.json",
            "business-logic-invariants.schema.json",
        ):
            shared = (ROOT / "shared/references" / name).read_bytes()
            for port_name, port in mappings.items():
                self.assertEqual((port / "references" / name).read_bytes(), shared, f"{port_name}:{name}")

    def test_every_packaged_owasp_graph_note_path_exists(self):
        mappings = {
            "claude": ROOT / "claude",
            "codex": ROOT / "codex/plugins/nightfalcon",
            "cursor": ROOT / "cursor",
        }
        for name, port in mappings.items():
            graph = json.loads((port / "references/OWASP/_graph.json").read_text())
            for framework in graph["frameworks"]:
                note_path = framework.get("note_path")
                self.assertIsInstance(note_path, str, f"{name}:{framework['id']}")
                self.assertTrue((port / note_path).is_file(), f"{name}:{framework['id']}:{note_path}")

    def test_required_owasp_category_sets_are_current(self):
        index = (ROOT / "shared/owasp/OWASP Framework Index.md").read_text()
        cloud = (ROOT / "shared/owasp/OWASP_Cloud_Agentic_and_Identity.md").read_text()
        for prefix, count, corpus in (
            ("A", 10, index), ("API", 10, index), ("M", 10, index),
            ("LLM", 10, index), ("ASI", 10, index), ("BLA", 10, index),
            ("K", 10, index), ("NHI", 10, cloud),
            ("SC", 10, index),
        ):
            for number in range(1, count + 1):
                width = 2 if prefix in {"A", "LLM", "ASI", "K", "MCP", "SC"} else 1
                self.assertRegex(corpus, rf"\b{prefix}{number:0{width}d}\b")
        self.assertIn("official OWASP MCP Top 10", cloud)

    def test_owasp_dispatch_and_high_risk_mappings_use_latest_editions(self):
        graph = json.loads((ROOT / "shared/owasp/_graph.json").read_text())
        framework_ids = {item["id"] for item in graph["frameworks"]}
        self.assertTrue({"web-2025", "llm-2026", "agentic-2026", "api-2023"} <= framework_ids)
        self.assertEqual(graph["system_kind_index"]["llm"], ["llm-2026"])
        self.assertIn("business-logic-abuse-2025", graph["system_kind_index"]["cross_domain"])
        table = (ROOT / "claude/references/pattern-validation-table.md").read_text()
        self.assertIn("Server-side request forgery (SSRF)** | CWE-918 | A01:2025", table)
        self.assertIn("Prompt injection (direct or indirect)** | CWE-1039 | LLM01:2026", table)
        self.assertIn("Insecure LLM output handling", table)
        self.assertIn("LLM10:2026", table)
        self.assertNotIn("OWASP 2021", table)

    def test_public_contract_markers_present_in_every_port(self):
        required = (
            "multitenant_scope",
            "relationship_context",
            "business_logic_invariants",
            "cross_repository_topology",
            "dependency_inventory",
        )
        for port in PORTS:
            corpus = "\n".join(
                p.read_text(errors="ignore") for p in port.rglob("*") if p.is_file()
            )
            for marker in required:
                self.assertIn(marker, corpus, f"{marker} missing from {port.name}")

    def test_dependency_inventory_is_phase_one_gate_artifact(self):
        scripts = (
            ROOT / "claude/scripts/validate-phase.sh",
            ROOT / "codex/plugins/nightfalcon/scripts/validate-phase.sh",
            ROOT / "cursor/scripts/validate-phase.sh",
        )
        for script in scripts:
            text = script.read_text()
            self.assertIn("dependency-inventory-${DATE}.json", text, script)
            self.assertIn("dependency_inventory", text, script)
            self.assertIn("package_manager_execution", text, script)
            self.assertIn("network_access", text, script)

    def test_v1_adapter_is_non_mutating(self):
        adapter = ROOT / "shared" / "compat" / "workspace_v1.py"
        fixture = ROOT / "tests" / "fixtures" / "workspace-v1" / "state.json"
        before = fixture.read_bytes()
        completed = subprocess.run(
            ["python3", str(adapter), "--input", str(fixture)],
            check=True,
            capture_output=True,
            text=True,
        )
        normalized = json.loads(completed.stdout)
        self.assertEqual(fixture.read_bytes(), before)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertIn("multitenant_scope", normalized)
        self.assertNotIn("org_scope", normalized)


if __name__ == "__main__":
    unittest.main()
