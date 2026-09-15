import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKTREE_ROOT = ROOT.parents[2]
ALL_PORT_ROOTS = (
    WORKTREE_ROOT / "claude",
    ROOT,
    WORKTREE_ROOT / "cursor",
)
PORT_ROOTS = tuple(
    root for root in ALL_PORT_ROOTS if root == ROOT or root.exists()
)
RUNTIME_MARKDOWN = [
    ROOT / "skills" / "nightfalcon" / "SKILL.md",
    *sorted((ROOT / "phases").glob("*.md")),
    *sorted((ROOT / "references").glob("*.md")),
]
BANNED = (
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DIR",
    "claude-sonnet",
    "claude-haiku",
    "claude-opus",
    "WebSearch",
    "WebFetch",
    "/reload-plugins",
    "via the Agent tool",
)


def orchestrator_text(root):
    if root == ROOT:
        return (root / "skills" / "nightfalcon" / "SKILL.md").read_text()
    if root.name == "cursor":
        return (root / ".cursor" / "skills" / "nightfalcon" / "SKILL.md").read_text()
    return (root / "skills" / "nightfalcon" / "SKILL.md").read_text()


def port_file(root, relative):
    return root / relative


def section(text, heading):
    start = text.index(heading)
    end = text.find("\n---", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def section_between(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    return text[start:end]


def heading_section(text, heading):
    """Return one Markdown heading section without leaking into peers."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    next_heading = re.search(rf"(?m)^#{{1,{level}}} ", text[start + len(heading) :])
    end = start + len(heading) + next_heading.start() if next_heading else len(text)
    return text[start:end]


def fenced_block_after(text, marker):
    start = text.index(marker)
    fence_start = text.index("```", start)
    fence_end = text.index("```", fence_start + 3)
    return text[fence_start + 3 : fence_end]


def journal_command_for_event(text, event):
    event_start = text.index(f"--event-type {event}")
    command_start = text.rfind("python3 ", 0, event_start)
    command_end = text.find("\n\n", event_start)
    return text[command_start:] if command_end == -1 else text[command_start:command_end]


ROLE_ALLOWED_TEXT = {
    "Judge": (
        "Allowed: only the finding's stable `F-NNN` ID, reviewer labels `A` and `B`, "
        "each reviewer `verdict`, `accuracy_ok`, `safety_ok`, and `completeness_ok` "
        "fields, and the retry-round scalar. Do not pass reviewer reasoning or fixes."
    ),
    "Generator": (
        "Allowed: only the finding's stable `F-NNN` ID, final disposition enum, "
        "one-sentence claim, candidate-embedded evidence excerpt, selected `script_type`, "
        "and `placeholders[]`."
    ),
}


def role_allowed_text(role_section):
    allowed_start = role_section.index("Allowed:")
    allowed_end = role_section.index("Must not receive:", allowed_start)
    return " ".join(role_section[allowed_start:allowed_end].split())


def assert_closed_role_allowlist(role, role_section):
    actual = role_allowed_text(role_section)
    expected = ROLE_ALLOWED_TEXT[role]
    if actual != expected:
        raise AssertionError(f"{role} allowlist was not exact: {actual!r}")


def assert_single_child_ledger_agent_id(command):
    agent_ids = re.findall(r'--agent-id\s+("[^"]+"|\S+)', command)
    if agent_ids != ['"$CHILD_LEDGER_ID"']:
        raise AssertionError(f"expected one child ledger agent ID, got {agent_ids!r}")
    if "$RUNTIME_ID" in command:
        raise AssertionError("runtime ID must not be used as a journal agent ID")


class PromptContractTests(unittest.TestCase):
    @staticmethod
    def skill_section(skill, heading):
        start = skill.index(heading)
        end = skill.find("\n## ", start + len(heading))
        return skill[start:] if end == -1 else skill[start:end]

    def test_orchestrators_record_agent_lifecycle_but_never_replay_journal(self):
        for root in PORT_ROOTS:
            skill = orchestrator_text(root)
            self.assertIn('agent-journal.py" record', skill)
            self.assertIn("agent-spawn-requested", skill)
            self.assertIn("agent-completed", skill)
            self.assertIn("agent-failed", skill)
            self.assertNotIn("pass the full agent conversation journal", skill.lower())

    def test_blind_context_sections_forbid_journal_content(self):
        for root in PORT_ROOTS:
            context = (root / "references" / "context-scope.md").read_text()
            self.assertIn("agent conversation journal content", context)
            self.assertIn("must not receive", context)

    def test_context_projection_precedes_authoritative_state_and_workers_get_closed_envelopes(self):
        for root in PORT_ROOTS:
            init = self.skill_section(orchestrator_text(root), "## Step 0")
            self.assertRegex(
                init,
                re.compile(
                    r"init-review\.sh.*agent-journal\.py\" context.*Then\s+read `state\.json`.*authoritative",
                    re.S,
                ),
                root.name,
            )
            context = port_file(root, "references/context-scope.md").read_text()
            boundary = section(context, "## Agent-journal boundary")
            self.assertIn("WORKER_STATE_ENVELOPE", boundary, root.name)
            self.assertIn("session_id", boundary, root.name)
            self.assertIn("current_phase", boundary, root.name)
            self.assertIn("epoch_id", boundary, root.name)
            self.assertNotIn("`state.json` is included", context, root.name)
            phase4 = section(context, "## phase-4")
            self.assertIn("RUN_MODE", phase4, root.name)
            lifecycle = self.skill_section(orchestrator_text(root), "## Agent journal lifecycle")
            expected_ledger = (
                "active-agent ledger must be empty before phase acceptance"
                if root == ROOT
                else "open-agent ledger must be empty before phase acceptance"
            )
            self.assertIn(expected_ledger, lifecycle, root.name)

    def test_step0_records_intent_after_authoritative_review_or_triage_input(self):
        for root in PORT_ROOTS:
            init = self.skill_section(orchestrator_text(root), "## Step 0")
            required_markers = (
                'agent-journal.py" context',
                "Then\n   read `state.json` and `output/session-manifest.json` as the authoritative",
                "On a fresh run `init-review.sh` seeds all",
                "Establish the authoritative run input before recording intent:",
                "For a review run, write `<workspace>/input/repos-<date>.txt`",
                "For a triage run, copy the user's findings file to",
                'agent-journal.py" record-intent',
                "Load `state.json` — read `current_phase`",
            )
            for marker in required_markers:
                self.assertIn(marker, init, f"{root.name}: {marker}")
            context_index = init.index('agent-journal.py" context')
            authoritative_index = init.index(
                "Then\n   read `state.json` and `output/session-manifest.json` as the authoritative"
            )
            repo_setup_index = init.index("On a fresh run `init-review.sh` seeds all")
            input_index = init.index(
                "Establish the authoritative run input before recording intent:"
            )
            review_input_index = init.index(
                "For a review run, write `<workspace>/input/repos-<date>.txt`"
            )
            triage_input_index = init.index(
                "For a triage run, copy the user's findings file to"
            )
            intent_index = init.index(
                'agent-journal.py" record-intent'
            )
            final_load_index = init.index(
                "Load `state.json` — read `current_phase`", intent_index
            )
            self.assertLess(
                context_index,
                authoritative_index,
                f"{root.name} context must precede authoritative reads",
            )
            self.assertLess(authoritative_index, repo_setup_index, root.name)
            self.assertLess(repo_setup_index, input_index, root.name)
            self.assertLess(input_index, review_input_index, root.name)
            self.assertLess(input_index, triage_input_index, root.name)
            self.assertLess(review_input_index, intent_index, root.name)
            self.assertLess(triage_input_index, intent_index, root.name)
            self.assertLess(intent_index, final_load_index, root.name)
            self.assertEqual(init.count("record-intent"), 1, root.name)

            command_start = init.rfind("```bash", 0, intent_index)
            command_end = init.index("```", intent_index)
            command = init[command_start:command_end]
            self.assertEqual(
                re.findall(r"(?<!\S)--[a-z-]+", command),
                ["--workspace"],
                root.name,
            )
            self.assertIn("every fresh or resumed invocation", init, root.name)
            self.assertIn("idempotent", init, root.name)
            self.assertIn("repository URLs", init, root.name)
            self.assertIn("raw prompt", init, root.name)

            triage = self.skill_section(
                orchestrator_text(root), "### Triage mode"
            )
            self.assertIn(
                "Step 9 has already copied the user's findings file",
                triage,
                root.name,
            )

    def test_step0_preserves_authoritative_state_on_resume_in_every_port(self):
        markers = (
            "Record whether `<workspace>/state.json` already exists before initialization",
            "canonical `state.json.date`",
            "Never replace a persisted run date with today's date",
            "On a fresh run `init-review.sh` seeds all",
            "existing state byte-for-byte",
        )
        for root in PORT_ROOTS:
            init = self.skill_section(orchestrator_text(root), "## Step 0")
            for marker in markers:
                self.assertIn(marker, init, f"{root.name}: {marker}")

    def test_phase_coordinators_own_complete_typed_child_lifecycle_records(self):
        mappings = (
            ("agent-spawn-requested", "spawn-agent", "requested", "phase-contract"),
            ("agent-started", "start-agent", "started", "phase-contract"),
            ("agent-completed", "complete-agent", "completed", "phase-contract"),
            ("agent-failed", "fail-agent", "failed", "worker-failure"),
            ("retry-scheduled", "schedule-retry", "scheduled", "retry-policy"),
        )
        for root in PORT_ROOTS:
            for phase, role in (("phase-4", "devils-advocate"), ("phase-6", "poc-reviewer")):
                prompt = port_file(root, f"phases/{phase}.md").read_text()
                lifecycle = section_between(
                    prompt,
                    "Coordinator-only journal lifecycle (record templates)",
                    "**DA isolation is a hard rule:**" if phase == "phase-4" else "- Omit the",
                )
                normalized_lifecycle = " ".join(lifecycle.split())
                self.assertIn("only coordinator writes lifecycle", normalized_lifecycle, root.name)
                self.assertIn(
                    'JOURNAL_SESSION_ID="$WORKER_STATE_ENVELOPE_SESSION_ID"',
                    lifecycle,
                    f"{root.name} {phase}",
                )
                self.assertIn(
                    'JOURNAL_PHASE="$WORKER_STATE_ENVELOPE_CURRENT_PHASE"',
                    lifecycle,
                    f"{root.name} {phase}",
                )
                self.assertIn(
                    'JOURNAL_EPOCH_ID="$WORKER_STATE_ENVELOPE_EPOCH_ID"',
                    lifecycle,
                    f"{root.name} {phase}",
                )
                self.assertIn(
                    "Keep `CHILD_LEDGER_ID` stable from spawn-requested through started,",
                    lifecycle,
                    f"{root.name} {phase}",
                )
                self.assertIn("runtime ID separately", normalized_lifecycle, f"{root.name} {phase}")
                self.assertNotIn(
                    "Map `CHILD_LEDGER_ID` to the runtime ID", lifecycle, f"{root.name} {phase}"
                )
                for event, action, status, reason in mappings:
                    command = journal_command_for_event(lifecycle, event)
                    self.assertIn(f"--event-type {event}", command, f"{root.name} {phase}")
                    self.assertIn(
                        '--session-id "$JOURNAL_SESSION_ID" --phase "$JOURNAL_PHASE" '
                        '--epoch-id "$JOURNAL_EPOCH_ID"',
                        command,
                        f"{root.name} {phase}",
                    )
                    self.assertIn(
                        f'--parent-agent-id "$COORDINATOR_LEDGER_ID" --agent-role {role} '
                        '--agent-id "$CHILD_LEDGER_ID"',
                        command,
                        f"{root.name} {phase}",
                    )
                    self.assertIn(
                        f"--action-code {action} --status-code {status} --reason-code {reason}",
                        command,
                        f"{root.name} {phase}",
                    )

    def test_lifecycle_commands_reject_runtime_or_duplicate_agent_ids(self):
        for root in PORT_ROOTS:
            for phase in ("phase-4", "phase-6"):
                prompt = port_file(root, f"phases/{phase}.md").read_text()
                lifecycle = section_between(
                    prompt,
                    "Coordinator-only journal lifecycle (record templates)",
                    "**DA isolation is a hard rule:**" if phase == "phase-4" else "- Omit the",
                )
                for event in (
                    "agent-spawn-requested",
                    "agent-started",
                    "agent-completed",
                    "agent-failed",
                    "retry-scheduled",
                ):
                    command = journal_command_for_event(lifecycle, event)
                    assert_single_child_ledger_agent_id(command)
                    with self.assertRaises(AssertionError):
                        assert_single_child_ledger_agent_id(
                            command.replace('"$CHILD_LEDGER_ID"', '"$RUNTIME_ID"')
                        )
                    with self.assertRaises(AssertionError):
                        assert_single_child_ledger_agent_id(
                            command.replace(
                                '"$CHILD_LEDGER_ID"',
                                '"$CHILD_LEDGER_ID" --agent-id "$CHILD_LEDGER_ID"',
                            )
                        )

    def test_authoritative_worker_packets_pass_closed_tokens_not_state_or_journal(self):
        for root in PORT_ROOTS:
            skill = orchestrator_text(root)
            phase4_packet = fenced_block_after(
                self.skill_section(skill, "## Phase 4 "), "**Pass to subagent (per slug):**"
            )
            phase6_packet = fenced_block_after(
                self.skill_section(skill, "## Phase 6 "), "**Pass to subagent (per slug):**"
            )
            for phase, packet in (("phase-4", phase4_packet), ("phase-6", phase6_packet)):
                self.assertIn("WORKER_STATE_ENVELOPE:", packet, f"{root.name} {phase}")
                self.assertIn("session_id", packet, f"{root.name} {phase}")
                self.assertIn("current_phase", packet, f"{root.name} {phase}")
                self.assertIn("epoch_id", packet, f"{root.name} {phase}")
                self.assertIn("COORDINATOR_LEDGER_ID:", packet, f"{root.name} {phase}")
                self.assertNotIn("state.json", packet, f"{root.name} {phase}")
                self.assertNotIn("journal", packet.lower(), f"{root.name} {phase}")
            self.assertIn("RUN_MODE: review | triage", phase4_packet, root.name)
            self.assertIn("source-less triage: triage", phase4_packet, root.name)

    def test_phase4_requires_run_mode_and_never_defaults_source_less_triage_to_review(self):
        for root in PORT_ROOTS:
            phase4 = port_file(root, "phases/phase-4.md").read_text()
            inputs = section(phase4, "## Inputs (provided in your context)")
            self.assertIn("WORKER_STATE_ENVELOPE", inputs, root.name)
            self.assertIn("COORDINATOR_LEDGER_ID", inputs, root.name)
            self.assertIn("RUN_MODE", inputs, root.name)
            self.assertIn("required", inputs.lower(), root.name)
            self.assertIn("source-less triage", inputs, root.name)
            routing = section(phase4, "## Candidate routing")
            self.assertIn("RUN_MODE == triage", routing, root.name)
            self.assertIn("RUN_MODE is absent: stop", routing, root.name)
            self.assertNotIn("(or absent)", routing, root.name)

    def test_phase4_extraction_branches_on_run_mode_in_every_port(self):
        for root in PORT_ROOTS:
            phase4 = port_file(root, "phases/phase-4.md").read_text()
            extraction = section_between(
                phase4, "### Step 2: Extract the code excerpt", "### Step 3:"
            )
            self.assertIn("RUN_MODE == review", extraction, root.name)
            self.assertIn("`SOURCE_PATH` is required", extraction, root.name)
            self.assertIn("RUN_MODE == triage", extraction, root.name)
            self.assertIn("candidate-embedded code excerpt", extraction, root.name)
            self.assertIn("EVIDENCE_UNAVAILABLE", extraction, root.name)
            self.assertIn("never re-open source", extraction, root.name)

    def test_blind_role_matrix_has_exact_allowlists_and_journal_denylists(self):
        denials = (
            "journal path",
            "journal content",
            "context projection",
            "full state.json",
            "run log",
            "parent conversation",
        )
        for root in PORT_ROOTS:
            context = port_file(root, "references/context-scope.md").read_text()
            matrix = (
                section(context, "## Blind child role matrix")
                if "## Blind child role matrix" in context
                else ""
            )
            self.assertIn("## Blind child role matrix", context, root.name)
            role_allowlists = {
                "Blind DA": ("phase-4 DA prompt", "one code/evidence excerpt", "current-round Primary statement"),
                "Judge": (
                    "finding's stable `F-NNN` ID",
                    "reviewer labels `A` and `B`",
                    "`accuracy_ok`, `safety_ok`, and `completeness_ok`",
                    "retry-round scalar",
                ),
                "Generator": (
                    "finding's stable `F-NNN` ID",
                    "final disposition enum",
                    "one-sentence claim",
                    "candidate-embedded evidence excerpt",
                    "selected `script_type`",
                    "`placeholders[]`",
                ),
                "Reviewer": ("phase-6 reviewer prompt", "script content", "reviewer label"),
            }
            for role, allowlist in role_allowlists.items():
                role_section = heading_section(matrix, f"### {role}") if f"### {role}" in matrix else ""
                self.assertIn("Allowed:", role_section, f"{root.name} {role}")
                self.assertIn("Must not receive:", role_section, f"{root.name} {role}")
                normalized_role_section = " ".join(role_section.split())
                for allowed in allowlist:
                    self.assertIn(allowed, normalized_role_section, f"{root.name} {role}")
                for denial in denials:
                    self.assertIn(denial, normalized_role_section, f"{root.name} {role}")
            self.assertNotIn("typed verdict envelopes", heading_section(matrix, "### Judge"), root.name)
            self.assertNotIn("documented phase-6 generator packet", heading_section(matrix, "### Generator"), root.name)

    def test_judge_and_generator_allowlists_are_closed_and_mutation_sensitive(self):
        for root in PORT_ROOTS:
            context = port_file(root, "references/context-scope.md").read_text()
            matrix = section(context, "## Blind child role matrix")
            for role in ("Judge", "Generator"):
                role_section = heading_section(matrix, f"### {role}")
                assert_closed_role_allowlist(role, role_section)
                if role == "Judge":
                    weakened = role_section.replace(" Do not pass reviewer reasoning or fixes.", "")
                    appended = role_section.replace(
                        "retry-round scalar.", "retry-round scalar, and full repository source."
                    )
                else:
                    weakened = role_section.replace("selected `script_type`", "")
                    appended = role_section.replace(
                        "and `placeholders[]`.", "and `placeholders[]`, and full repository source."
                    )
                with self.assertRaises(AssertionError):
                    assert_closed_role_allowlist(role, weakened)
                with self.assertRaises(AssertionError):
                    assert_closed_role_allowlist(role, appended)

    def test_skill_frontmatter_is_codex_native(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        frontmatter = skill.split("---", 2)[1]
        self.assertIn("name: nightfalcon", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertNotRegex(frontmatter, r"(?m)^model:")

    def test_runtime_markdown_has_no_executable_claude_dependencies(self):
        for path in RUNTIME_MARKDOWN:
            text = path.read_text(errors="ignore")
            for token in BANNED:
                self.assertNotIn(token, text, f"{token} in {path.relative_to(ROOT)}")

    def test_orchestrator_requires_codex_depth_and_preflight(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        self.assertIn("max_depth = 5", skill)
        self.assertIn('python3 "$PLUGIN_ROOT/scripts/preflight.py"', skill)
        self.assertNotIn('preflight.py" --workspace', skill)
        self.assertIn("PLUGIN_ROOT", skill)
        self.assertIn("spawn", skill.lower())
        self.assertIn("wait", skill.lower())
        self.assertIn("fresh", skill.lower())

    def test_agent_threads_support_explicit_close_and_v2_auto_eviction(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        for label, text in {"skill": skill, "phase-4": phase4, "phase-6": phase6}.items():
            self.assertIn("AGENT_LIFECYCLE_MODE", text, label)
            self.assertIn("explicit-close", text, label)
            self.assertIn("auto-evict", text, label)
            self.assertIn("close_agent", text, label)
            self.assertIn("agent thread limit reached", text, label)
            self.assertIn("terminal", text, label)
        self.assertIn("v2 auto-evicts terminal agents", skill)
        self.assertIn("agents.max_threads", skill)
        self.assertIn("Run Phase-4 repo workers sequentially", skill)
        self.assertIn("Run Phase-6 repo workers sequentially", skill)
        for label, text in {"phase-4": phase4, "phase-6": phase6}.items():
            self.assertNotIn("same blind leaf to re-emit", text, label)
            self.assertIn("fresh blind leaf", text, label)
            self.assertIn("Retain every successful child ID", text, label)
            self.assertIn("retry only the failed child queue item once", text, label)
            self.assertIn("Never re-spawn a successful sibling", text, label)

    def test_orchestrator_keeps_active_agent_ledger_for_both_lifecycles(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        normalized = " ".join(skill.split())
        self.assertIn("ACTIVE_AGENT_IDS", skill)
        self.assertIn(
            "available_slots = MAX_AGENT_THREADS - len(ACTIVE_AGENT_IDS)", normalized
        )
        self.assertIn(
            "ACTIVE_AGENT_IDS must be empty before `complete-phase.sh`", normalized
        )
        self.assertIn("Remove an ID only after its terminal response", normalized)
        self.assertNotIn("OPEN_AGENT_IDS", skill)

    def test_lifecycle_orders_v1_close_and_contains_v2_wait_failure(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        for label, text in {"skill": skill, "phase-4": phase4, "phase-6": phase6}.items():
            normalized = " ".join(text.split())
            self.assertIn(
                "close the terminal thread before recording its terminal journal event",
                normalized,
                label,
            )
            self.assertIn("interrupt_agent", text, label)
            self.assertIn("wait failure", text.lower(), label)
            self.assertIn("spawn-rejected", text, label)
        self.assertIn("list_agents", skill)
        self.assertIn("followup_task", skill)

    def test_codex_phase_transition_documents_automatic_intent_seed(self):
        init = self.skill_section(orchestrator_text(ROOT), "## Step 0")
        normalized = " ".join(init.split())
        self.assertIn("Each successful nonterminal `complete-phase.sh`", normalized)
        self.assertIn("seeds exactly one canonical intent", normalized)
        self.assertIn(
            "do not repeat the manual intent-recording command between phases", init
        )
        self.assertIn("durable exact-prefix transaction", init)
        self.assertIn("torn writes are repaired before journal parsing", init)

    def test_resume_does_not_claim_runtime_cleanup_from_journal_only(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        normalized = " ".join(skill.split())
        self.assertIn("default reconciliation must BLOCK", normalized)
        self.assertNotIn("--runtime-cleanup-confirmed", skill)
        self.assertIn("exact terminal journal event", normalized)
        self.assertIn("never synthesize terminal evidence", normalized)
        self.assertIn(
            "Never infer prior-root cleanup from an empty current-root `list_agents` result",
            normalized,
        )
        self.assertNotIn("otherwise resume from a new root task", skill)

    def test_resume_queues_only_repo_outputs_that_do_not_validate(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        self.assertIn("--check-slug <slug>", skill)
        self.assertIn("Exit `0`: do not spawn that repo worker", skill)
        self.assertIn("Exit `2`: add that slug to the pending queue", skill)
        self.assertIn("does not satisfy the phase gate", skill)

    def test_file_preflight_does_not_claim_to_see_task_start_overrides(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        self.assertIn("file-resolved", skill)
        self.assertIn("task-start profile", skill)
        self.assertIn("runtime agent errors are authoritative", skill)

    def test_blindness_survives_and_phase7_uses_structured_projections(self):
        da = (ROOT / "phases" / "phase-da.md").read_text()
        reviewer = (ROOT / "phases" / "phase-poc-reviewer.md").read_text()
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        self.assertIn("BLIND_VIOLATION", da)
        self.assertIn("BLIND_VIOLATION", reviewer)
        self.assertNotIn("===BEGIN FINDINGS REPORT===", phase7)
        self.assertIn("build-markdown.py", phase7)

    def test_phase7_and_phase8_emit_sarif_via_builder(self):
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        phase8 = (ROOT / "phases" / "phase-8.md").read_text()
        # Both must build SARIF mechanically via the shared converter, not
        # hand-author it.
        self.assertIn("build-sarif.py", phase7)
        self.assertIn("build-sarif.py", phase8)
        self.assertIn("findings-<DATE>.sarif", phase7)
        self.assertIn("aggregate SARIF", phase8)
        self.assertIn("deterministic", phase7)
        self.assertIn("deterministic", phase8)
        # The converter script exists and is stdlib-only.
        self.assertTrue((ROOT / "scripts" / "report" / "build-sarif.py").exists())

    def test_all_spawns_inherit_user_selected_model_without_harness_routing(self):
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        schema = (ROOT / "references" / "state-schema.md").read_text()
        enums = (ROOT / "references" / "enums.md").read_text()
        hook_setup = (ROOT / "references" / "hook-setup.md").read_text()
        combined = "\n".join((skill, phase4, phase6, schema, enums, hook_setup))
        normalized_skill = " ".join(skill.split())
        invocation = self.skill_section(skill, "## Invocation arguments")
        resume = self.skill_section(skill, "## On Resume (interrupted run)")

        for forbidden in (
            "EXPECTED_MODEL",
            "NIGHTFALCON_MODEL_LOCK",
            "NIGHTFALCON_MODEL_MISMATCH",
            "NIGHTFALCON_CHANGE_MODEL",
            "NIGHTFALCON_MODEL_REPINNED",
        ):
            self.assertNotIn(forbidden, "\n".join(
                path.read_text(errors="ignore") for path in RUNTIME_MARKDOWN
            ))

        self.assertIn("NightFalcon never selects or switches models", normalized_skill)
        self.assertIn("model currently selected by the user", normalized_skill)
        self.assertIn("A user may change that selection at any time", normalized_skill)
        self.assertIn("Already-running agents keep their launch model", normalized_skill)
        self.assertIn("later turns and new agents use the new selection", normalized_skill)
        self.assertIn(
            "Never reject, retry, or reroute work because the observed model differs",
            normalized_skill,
        )
        self.assertNotRegex(combined, re.compile(r"spawn_agent\([^)]*\bmodel\s*=", re.S))
        self.assertNotRegex(combined, re.compile(r"set [`]?model(?:=| to)", re.I))
        self.assertIn("omit the `model` argument", normalized_skill.lower())
        self.assertNotIn("phase_models", combined)
        self.assertNotIn("phase_models", schema)
        self.assertIn("initial provenance only", schema.lower())
        self.assertNotIn("fallback chain", phase6.lower())
        self.assertNotRegex(phase6, re.compile(r"sonnet|haiku|opus", re.I))
        self.assertNotIn("rereads `state.json.model`", phase6)
        self.assertNotIn("requires the exact SubagentStart verification", phase6)
        self.assertIn("Thread-capacity recovery does not consume a generation attempt", skill)
        self.assertNotIn("If the fresh spawn or wait fails:", skill)

        phase4_nested_spawn = phase4[
            phase4.index("2. **Spawn a NEW, isolated DA subagent"):
            phase4.index("**DA isolation is a hard rule:**")
        ]
        phase6_nested_spawn = phase6[
            phase6.index("**Spawn each reviewer with:**"):
            phase6.index("**Do NOT pass to either reviewer:**")
        ]
        for label, text in {
            "phase-4 DA": phase4_nested_spawn,
            "phase-6 reviewer": phase6_nested_spawn,
        }.items():
            normalized = " ".join(text.split())
            self.assertIn("Omit the `model` argument and every reasoning override", normalized, label)
            self.assertIn("model currently selected by the user", normalized, label)
        self.assertNotRegex(phase4_nested_spawn, re.compile(r"model metadata", re.I))
        self.assertNotRegex(phase6_nested_spawn, re.compile(r"model metadata", re.I))

        for label, text in {
            "orchestrator": skill,
            "phase-4 DA": phase4,
            "phase-6 reviewer": phase6,
        }.items():
            self.assertIn(
                "Omit the `model` argument and every reasoning override",
                " ".join(text.split()),
                label,
            )

        for label, section in {"invocation": invocation, "resume": resume}.items():
            normalized = " ".join(section.split())
            self.assertIn("may change", normalized, label)
            self.assertIn("Already-running agents keep their launch model", normalized, label)
            self.assertIn("new agents use the new selection", normalized, label)

        for path in sorted((ROOT / "phases").glob("*.md")):
            prompt = " ".join(path.read_text().split())
            self.assertIn(
                "Omit the `model` argument and every reasoning override",
                prompt,
                path.name,
            )
            self.assertIn("model currently selected by the user", prompt, path.name)
            self.assertIn("Already-running agents keep their launch model", prompt, path.name)

        self.assertIn(
            "actual advisory Codex model reported to this worker, or `unknown`",
            " ".join(phase6.split()),
        )

    def test_authoritative_model_references_document_user_control_and_advisory_audit(self):
        references = {
            name: (ROOT / "references" / name).read_text()
            for name in ("state-schema.md", "enums.md", "hook-setup.md")
        }
        combined = "\n".join(references.values())

        for label, text in references.items():
            normalized = " ".join(text.split())
            self.assertIn(
                "Omit the `model` argument and every reasoning override",
                normalized,
                label,
            )
            self.assertIn("model currently selected by the user", normalized, label)
            self.assertIn("A user may change that selection at any time", normalized, label)
            self.assertIn("Already-running agents keep their launch model", normalized, label)
            self.assertIn("model_policy", normalized, label)

        self.assertNotIn("passes it exactly", combined)
        self.assertNotIn("Pass it through the spawn model field", combined)
        self.assertNotRegex(
            combined,
            re.compile(r"(?:set|pass|supply).{0,80}`?model`? (?:argument|field)", re.I | re.S),
        )

        schema = references["state-schema.md"]
        self.assertIn('"model_policy": "user-selected"', schema)
        self.assertIn('"model_history":', schema)
        self.assertIn("initial provenance only", schema.lower())
        self.assertIn("ignored and left untouched", schema.lower())
        self.assertNotIn('"model_epoch":', schema)
        self.assertNotIn('"model_lock":', schema)
        self.assertIn("NIGHTFALCON_MODEL_SELECTED", combined)
        self.assertIn("$PLUGIN_DATA/model-selections/", combined)

    def test_hook_manifest_keeps_lifecycle_and_path_enforcement_events(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("SubagentStart", hooks)
        self.assertIn("SubagentStop", hooks)
        pretool = hooks["PreToolUse"]
        self.assertEqual(len(pretool), 1)
        self.assertNotIn("matcher", pretool[0])

    def test_hook_status_messages_describe_passive_audit_lifecycle_and_write_checks(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]

        status_messages = {
            event: hooks[event][0]["hooks"][0]["statusMessage"]
            for event in ("UserPromptSubmit", "SubagentStart", "PreToolUse")
        }

        self.assertEqual(
            status_messages,
            {
                "UserPromptSubmit": "Recording NightFalcon passive model-selection audit",
                "SubagentStart": "Registering NightFalcon agent lifecycle",
                "PreToolUse": "Checking NightFalcon write path",
            },
        )
        prohibited = (
            "lock",
            "comparison",
            "enforcement",
            "switching",
            "routing",
            "fallback",
            "retry",
        )
        for message in status_messages.values():
            self.assertFalse(
                any(term in message.lower() for term in prohibited),
                message,
            )

    def test_hook_summary_documents_stop_cleanup_without_model_enforcement(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        enforcement = self.skill_section(skill, "## Hook Enforcement")
        self.assertIn("SubagentStop hook", enforcement)
        self.assertIn("active-agent registry", enforcement)
        self.assertIn("write-path", enforcement)
        self.assertIn("never enforce", enforcement.lower())

    def test_enum_model_example_is_initial_advisory_provenance_only(self):
        enums = (ROOT / "references" / "enums.md").read_text()
        initial = enums.split("### Initial SessionStart provenance", 1)[1].split(
            "NightFalcon never selects or switches models", 1
        )[0]
        self.assertIn('"model_policy": "user-selected"', initial)
        self.assertIn('"source": "SessionStart"', initial)
        self.assertNotIn("UserPromptSubmit", initial)
        self.assertNotIn('"epoch":', initial)
        self.assertNotIn("Repinned UserPromptSubmit epoch", enums)

    def test_phase6_refusal_retry_is_fresh_model_neutral_and_unlabelled(self):
        enums = (ROOT / "references" / "enums.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        combined = enums + "\n" + phase6
        retry = enums.split("- `generation-refused`", 1)[1].split(
            "- `generation-failed`", 1
        )[0]

        self.assertNotIn("same inherited model", combined.lower())
        self.assertNotRegex(
            combined,
            re.compile(r"(?:also )?refused by <[^>]*model[^>]*>", re.I),
        )
        self.assertIn("fresh", retry.lower())
        self.assertIn("omit", retry.lower())
        self.assertIn("user", retry.lower())
        self.assertIn("launch", retry.lower())
        self.assertIn("never compares", retry.lower())

    def test_nested_spawns_disable_parent_context_forking(self):
        contracts = {
            "skill": (
                (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text(),
                "Every phase worker spawn MUST set `fork_turns=\"none\"`",
                "Pass only the documented phase context packet",
            ),
            "phase-4": (
                (ROOT / "phases" / "phase-4.md").read_text(),
                "Every DA spawn and re-spawn MUST set `fork_turns=\"none\"`",
                "Pass only the documented blind DA packet",
            ),
            "phase-6": (
                (ROOT / "phases" / "phase-6.md").read_text(),
                "Every PoC reviewer spawn and re-spawn MUST set `fork_turns=\"none\"`",
                "Pass only the documented blind reviewer packet",
            ),
        }
        for label, (text, isolation, packet) in contracts.items():
            self.assertIn(isolation, text, label)
            self.assertIn(packet, text, label)
            self.assertNotIn('fork_turns="all"', text, label)

    def test_orchestrator_runs_phases_4_to_6_for_every_repo(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        for phase in (4, 5, 6):
            section = self.skill_section(skill, f"## Phase {phase} ")
            self.assertIn("every repo slug", section.lower(), f"phase-{phase}")
            self.assertNotIn("only for", section.lower(), f"phase-{phase}")
        self.assertNotIn("If ALL repos have NO_CANDIDATES", skill)
        for flag in ("--no-debate-needed", "--no-validation-needed", "--no-poc-needed"):
            self.assertNotIn(flag, skill)

    def test_no_work_artifacts_are_per_repo(self):
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase5 = (ROOT / "phases" / "phase-5.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        self.assertIn("NO_DEBATE_CANDIDATES", phase4)
        self.assertIn("Final Disposition: NEEDS-REVIEW", phase4)
        self.assertIn("NO_VALIDATION_CANDIDATES", phase5)
        self.assertIn("Validation: NOT-RUN", phase5)
        self.assertIn("NO_POC_CANDIDATES", phase6)
        self.assertIn('"pocs": []', phase6)
        self.assertNotIn("--no-poc-needed", phase6)

    def test_phase4_p3_p4_only_path_carries_every_candidate_forward(self):
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        rationale = "Not debated by tier policy; carried forward under Non-Regression Principle"
        self.assertIn("If the repo has P3/P4 candidates but no P0/P1/P2", phase4)
        self.assertIn("update every P3/P4 candidate record", phase4)
        self.assertIn("- Final Disposition: NEEDS-REVIEW", phase4)
        self.assertIn(rationale, phase4)
        self.assertIn("Do not renumber", phase4)
        self.assertIn("every non-dismissed candidate exactly once", phase7)
        self.assertIn("post-debate candidate", phase7)
        self.assertIn("never removes a supported finding", phase7)

    def test_phase4_mixed_tier_path_carries_non_debated_candidates_forward(self):
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        rationale = "Not debated by tier policy; carried forward under Non-Regression Principle"
        self.assertIn("For a mixed-tier repo", phase4)
        self.assertIn("every non-debated P3/P4 candidate", phase4)
        self.assertIn("Final Disposition: NEEDS-REVIEW", phase4)
        self.assertIn(rationale, phase4)
        self.assertIn("carried-forward block", phase4)

    def test_phase4_debates_imported_p3_p4_before_review_carry_forward(self):
        ingest = (ROOT / "phases" / "triage-ingest.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        self.assertIn("Mark every candidate `Imported finding: YES`", ingest)
        self.assertIn("Mark each candidate **Debate required: YES**", ingest)
        self.assertIn(
            "Every candidate marked `Imported finding: YES` enters the debate queue",
            phase4,
        )
        self.assertIn(
            "regardless of its untrusted `tier_claimed` or current P0–P4 tier",
            phase4,
        )
        self.assertIn(
            "derive and write both `Final Tier` and `Final Disposition`",
            phase4,
        )
        self.assertIn(
            "Imported P3/P4 candidates must never enter the carry-forward path",
            phase4,
        )
        self.assertIn(
            "always debated even when its claimed/current tier is P3 or P4",
            phase4,
        )
        self.assertIn(
            "Ordinary review P3/P4 candidates marked `Debate required: NO`",
            phase4,
        )
        self.assertIn("## For each queued candidate", phase4)

    def test_imported_findings_use_derived_tier_for_recall_protection(self):
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        self.assertIn(
            "For imported findings, `tier_claimed` may only order the initial queue",
            phase4,
        )
        self.assertIn(
            "every staleness, recall-protection, early-termination, and disposition decision uses the current independently derived tier",
            phase4,
        )
        self.assertIn(
            "Establish the current independently derived tier before the first tier-sensitive decision",
            phase4,
        )
        self.assertIn(
            "re-evaluate it whenever debate evidence changes the score",
            phase4,
        )
        self.assertIn(
            "An imported finding claimed as P3/P4 but currently re-scored to P0/P1/P2 receives the P0/P1/P2 protection",
            phase4,
        )
        self.assertNotIn("**Imported P3 / P4 candidates:** disposition", phase4)

    def test_triage_uses_phase7_and_candidate_embedded_source(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        self.assertIn("when you spawn phase-7", skill)
        self.assertIn("omit `DATAFLOW_PATH`", skill)
        self.assertIn('`commit_sha` and `multitenant_scope` to `"n/a (triage)"`', skill)
        self.assertIn("`SOURCE_PATH` — optional", phase4)
        self.assertIn("source-less triage", phase4)
        self.assertIn("candidate-embedded code excerpt", phase4)
        self.assertIn("never re-open source", phase4)
        self.assertIn("`DATAFLOW_PATH` in review mode; omitted in source-less triage", phase7)
        self.assertIn("source-less triage", phase7)
        self.assertIn('`"n/a (triage)"`', phase7)

    def test_source_less_triage_preserves_claims_without_inventing_evidence(self):
        sentinel = "EVIDENCE_UNAVAILABLE — external finding supplied without repository/code excerpt"
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        ingest = (ROOT / "phases" / "triage-ingest.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        da = (ROOT / "phases" / "phase-da.md").read_text()
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        context_scope = (ROOT / "references" / "context-scope.md").read_text()
        for label, text in (("ingest", ingest), ("phase4", phase4), ("da", da), ("phase7", phase7)):
            self.assertIn(sentinel, text, label)
        self.assertIn("never invent source", ingest.lower())
        self.assertIn("cannot `DISMISS` solely because evidence is unavailable", phase4)
        self.assertIn("must not recommend `dismiss` solely because evidence is unavailable", da)
        self.assertIn("unresolved", phase4)
        self.assertIn("NEEDS-REVIEW", phase4)
        self.assertIn("`OWASP_CONTEXT_PATH` and `ORGANIZATION_CONTEXT_PATH` when available", phase7)
        self.assertIn("Not available — external finding supplied without framework context", phase7)
        self.assertIn("preserve claim-only findings", phase7.lower())
        self.assertIn("without inventing source", phase7)
        self.assertIn("an actor", phase7)
        self.assertIn("omit `DATAFLOW_PATH`, `OWASP_CONTEXT_PATH`, and `ORGANIZATION_CONTEXT_PATH`", skill)
        self.assertIn("omitted in source-less triage", context_scope)

    def test_phase5_operational_queue_includes_confirmed_modified(self):
        phase5 = (ROOT / "phases" / "phase-5.md").read_text()
        steps = phase5[phase5.index("## Steps"):]
        self.assertIn(
            "eligible candidates (CONFIRMED, CONFIRMED-MODIFIED, or NEEDS-REVIEW at P0 / P1 / P2)",
            steps,
        )

    def test_phase8_uses_verified_plugin_root(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase8 = (ROOT / "phases" / "phase-8.md").read_text()
        section = self.skill_section(skill, "## Phase 8 ")
        self.assertIn("PLUGIN_ROOT: <verified plugin root>", section)
        self.assertIn("- `PLUGIN_ROOT`", phase8)
        self.assertIn('<PLUGIN_ROOT>/scripts/report/build.py', phase8)
        self.assertNotIn('dirname "$0"', phase8)
        self.assertNotRegex(phase8, re.compile(r"PLUGIN_ROOT=.*dirname"))

    def test_context_scope_matches_phase_two_and_three_dataflow_artifacts(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase2_context = self.skill_section(context, "## phase-2 ")
        phase3_context = self.skill_section(context, "## phase-3 ")
        phase2_skill = self.skill_section(skill, "## Phase 2 ")
        phase3_skill = self.skill_section(skill, "## Phase 3 ")

        for label, section in (("context", phase2_context), ("skill", phase2_skill)):
            self.assertIn("DATAFLOW_JSON_PATH", section, label)
            self.assertIn("dataflow-<date>.json", section, label)
        self.assertIn(
            "`findings/<slug>/dataflow-<date>.json`",
            phase2_context,
        )
        self.assertIn("structured JSON projection", phase2_context)

        for label, section in (("context", phase3_context), ("skill", phase3_skill)):
            self.assertIn("DATAFLOW_JSON_PATH", section, label)
            self.assertIn("dataflow-<date>.json", section, label)
        self.assertNotIn("debate-<date>.md", phase3_context)
        self.assertNotIn("findings-<date>.md", phase3_context)

    def test_context_scope_documents_triage_ingest_packet_and_source_less_rules(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        self.assertIn("## triage-ingest ", context)
        section = self.skill_section(context, "## triage-ingest ")
        for token in (
            "WORKER_STATE_ENVELOPE",
            "BACKLOG_PATH",
            "input/triage-backlog.<ext>",
            "REPO_SLUGS",
            "REPO_PATH",
            "findings/<slug>/candidates-<date>.md",
            "output/run-log-<date>.md",
            "EVIDENCE_UNAVAILABLE — external finding supplied without repository/code excerpt",
        ):
            self.assertIn(token, section)
        self.assertIn("omitted in source-less triage", section)
        self.assertIn("Never invent", section)
        for forbidden in ("debate-<date>.md", "receipt-<date>.json", "pattern-tags-<date>.json"):
            self.assertNotIn(forbidden, section)

    def test_phase_six_context_has_self_contained_per_repo_pocs_under_output(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        section = self.skill_section(context, "## phase-6 ")
        # Self-contained PoCs live under output/proof_of_concept/<slug>/ with a
        # per-repo guide + reference catalog; scripts declare params inline.
        for token in (
            "POC_GUIDE_PATH",
            "output/proof_of_concept/<slug>/POC-GUIDE-<date>.md",
            "POC_CONFIG_PATH",
            "output/proof_of_concept/<slug>/poc-config.env",
            "POC_INVOKER_PATH",
            "output/proof_of_concept/run-all.sh",
            "POC_LIB_PATH",
            "output/proof_of_concept/lib/poc-common.sh",
            "output/proof_of_concept/<slug>/F-<NNN>-*.<ext>",
            "output/proof_of_concept/<slug>/poc-manifest-<date>.json",
        ):
            self.assertIn(token, section)
        # The catalog is documentation, never sourced at runtime.
        self.assertIn("never sourced at runtime", section)
        self.assertIn("self-contained", section.lower())
        # The old single-root workspace-level config path must be gone.
        self.assertNotIn("proof_of_concept/poc-config.env\n", section)

    def test_phase_six_excludes_source_path_but_keeps_runtime_recheckout_wiring(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        context_section = self.skill_section(context, "## phase-6 ")
        skill_section = self.skill_section(skill, "## Phase 6 ")
        inputs = phase6[phase6.index("## Inputs (provided in your context)"):phase6.index("## Eligible findings")]
        context_flat = " ".join(context_section.split())

        self.assertNotIn("SOURCE_PATH:", skill_section)
        self.assertNotIn("`SOURCE_PATH`", inputs)
        self.assertIn("no `SOURCE_PATH`", context_section)
        self.assertIn("must not directly open", context_flat)
        for variable in ("SOURCE_DIR_ROOT", "REPO_MAP_PATH"):
            self.assertIn(variable, skill_section)
            self.assertIn(f"`{variable}`", inputs)
            self.assertIn(variable, context_section)
        self.assertIn("generated PoC runtime and re-checkout wiring only", context_flat)
        self.assertIn("not Phase-6 analysis inputs", context_flat)

    def test_blind_leaves_signal_injection_to_parent_without_logging_or_context_expansion(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        phase4 = (ROOT / "phases" / "phase-4.md").read_text()
        phase6 = (ROOT / "phases" / "phase-6.md").read_text()
        da = (ROOT / "phases" / "phase-da.md").read_text()
        reviewer = (ROOT / "phases" / "phase-poc-reviewer.md").read_text()

        for label, leaf in (("DA", da), ("PoC reviewer", reviewer)):
            self.assertNotIn("output/run-log", leaf, label)
            self.assertNotIn("<DATE>", leaf, label)
            self.assertIn('"prompt_injection_detected": true | false', leaf, label)
            self.assertIn("required boolean", leaf.lower(), label)
            self.assertIn("continue the full normal", leaf.lower(), label)
            self.assertIn("BLIND_VIOLATION", leaf, label)
            self.assertIn("single line", leaf, label)
        self.assertNotIn("WORKSPACE", reviewer)

        for label, parent in (("phase-4", phase4), ("phase-6", phase6)):
            parent_flat = " ".join(parent.split())
            self.assertIn('"prompt_injection_detected": true | false', parent, label)
            self.assertIn("required boolean", parent.lower(), label)
            self.assertIn("output/run-log-<DATE>.md", parent, label)
            self.assertIn("append exactly one sanitized line", parent_flat.lower(), label)
            self.assertIn("never copy or paraphrase the attacker-controlled text", parent, label)
            self.assertIn("BLIND_VIOLATION", parent, label)

        da_scope = context[context.index("## What the DA Subagent Receives"):context.index("## What the PoC Reviewer Subagent Receives")]
        reviewer_scope = context[context.index("## What the PoC Reviewer Subagent Receives"):]
        for section in (da_scope, reviewer_scope):
            section_flat = " ".join(section.split())
            self.assertIn("prompt_injection_detected", section)
            self.assertIn("parent worker", section_flat.lower())
            self.assertIn("sanitized line", section_flat.lower())
            self.assertNotIn("<DATE>", section)
            self.assertNotIn("output/run-log", section)
        self.assertNotIn("<WORKSPACE", reviewer_scope)

        for forbidden in (
            "Scoring breakdown, tier, or justification",
            "Prior debate rounds",
            "Any other candidate or finding",
            "The orchestrator's or phase-4's conversation history",
        ):
            self.assertIn(forbidden, da_scope)
        for forbidden in (
            "Scoring breakdown, tier, or severity justification",
            "Debate transcript or prior phase output",
            "Other findings or other PoC scripts",
            "The other reviewer's verdict",
            "The contents of `poc-config.env`",
        ):
            self.assertIn(forbidden, reviewer_scope)

    def test_skill_phase_four_da_packet_excludes_parent_context(self):
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase4 = self.skill_section(skill, "## Phase 4 ")
        start = phase4.index("**DA isolation (hard rule).**")
        end = phase4.index("After every slug's worker", start)
        isolation = phase4[start:end]
        isolation_flat = " ".join(isolation.split())

        self.assertIn('fork_turns="none"', isolation_flat)
        for authorized in (
            "`phases/phase-da.md`",
            "`F-NNN`",
            "`finding_type`",
            "single code/evidence excerpt",
            "one-sentence claim",
            "Primary's statement for this round only",
            "round number",
        ):
            self.assertIn(authorized, isolation_flat)
        for forbidden in ("WORKSPACE", "DATE", "run-log", "log path"):
            self.assertNotIn(forbidden, isolation_flat)
        self.assertIn("scoring/tier/justification", isolation_flat)
        self.assertIn("prior rounds", isolation_flat)
        self.assertIn("phase-4 conversation history", isolation_flat)

    def test_phase_seven_contract_assigns_projections_to_orchestrator_and_json_to_worker(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        context_section = self.skill_section(context, "## phase-7 ")
        skill_section = self.skill_section(skill, "## Phase 7 ")
        variables = (
            "FINDINGS_JSON_PATH",
            "RECEIPT_PATH",
            "PATTERN_TAGS_PATH",
        )
        artifacts = (
            "findings-<date>.json",
            "receipt-<date>.json",
            "pattern-tags-<date>.json",
        )
        for token in variables:
            self.assertIn(token, context_section)
            self.assertIn(token, skill_section)
            self.assertIn(token, phase7)
        for token in artifacts:
            self.assertIn(token, context_section.lower())
            self.assertIn(token, skill_section.lower())
        for section in (context_section, skill_section, phase7):
            self.assertNotIn("===BEGIN FINDINGS REPORT===", section)
            self.assertIn("orchestrator", section.lower())
        self.assertIn("build-markdown.py", skill_section)
        self.assertIn("tagged-union", phase7)

    def test_phase_eight_context_enumerates_structured_inputs_and_outputs(self):
        context = (ROOT / "references" / "context-scope.md").read_text()
        skill = (ROOT / "skills" / "nightfalcon" / "SKILL.md").read_text()
        phase8 = (ROOT / "phases" / "phase-8.md").read_text()
        context_section = self.skill_section(context, "## phase-8 ")
        skill_section = self.skill_section(skill, "## Phase 8 ")
        required_variables = (
            "FINDINGS_JSON_PATHS",
            "PLUGIN_ROOT",
            "EXECUTIVE_REPORT_JSON_PATH",
            "EXECUTIVE_SUMMARY_PATH",
            "EXECUTIVE_REPORT_HTML_PATH",
            "EXECUTIVE_REPORT_SARIF_PATH",
        )
        required_artifacts = (
            "findings-<date>.json",
            "executive-report-<date>.json",
            "executive-summary-<date>.md",
            "executive-report-<date>.html",
            "executive-report-<date>.sarif",
        )
        for token in required_variables:
            self.assertIn(token, context_section, token)
            self.assertIn(token, skill_section, token)
            self.assertIn(token, phase8, token)
        for token in required_artifacts:
            self.assertIn(token, context_section.lower(), token)
            self.assertIn(token, skill_section.lower(), token)
        self.assertIn("Worker writes", context_section)
        self.assertIn("sole", context_section)
        self.assertNotIn("FINDINGS_MARKDOWN_PATHS", context_section)
        for forbidden_input in (
            "RECEIPT_PATHS", "PATTERN_TAG_PATHS", "STATE_DERIVED_SUMMARY_PATH"
        ):
            self.assertNotIn(forbidden_input, context_section)
            self.assertNotIn(forbidden_input, skill_section)
            self.assertNotIn(forbidden_input, phase8)
        for forbidden in (
            "candidates-<date>.md",
            "debate-<date>.md",
            "dataflow-<date>.md",
            "sourcecode/<slug>",
        ):
            self.assertNotIn(forbidden, context_section)

    def test_state_docs_treat_model_as_advisory_initial_provenance_and_show_complete_pipelines(self):
        schema = (ROOT / "references" / "state-schema.md").read_text()
        self.assertIn('"model_policy": "user-selected"', schema)
        self.assertIn("initial provenance only", schema.lower())
        self.assertIn("external", schema.lower())
        self.assertIn("$PLUGIN_DATA/model-selections/", schema)
        self.assertIn("ignored and left untouched", schema.lower())
        self.assertIn("newly initialized", schema.lower())
        self.assertIn("legacy state may omit", schema.lower())
        self.assertIn("`phase-0` → `phase-1` → `phase-2` → `phase-3` → `phase-4` → `phase-5` → `phase-6` → `phase-7` → `phase-8`", schema)
        self.assertIn("`triage-ingest` → `phase-4` → `phase-5` → `phase-6` → `phase-7` → `phase-8`", schema)

    def test_cvss_is_the_canonical_scoring_model(self):
        phase3 = (ROOT / "phases" / "phase-3.md").read_text()
        enums = (ROOT / "references" / "enums.md").read_text()
        policy = (ROOT / "references" / "cvss-policy.md")
        # The CVSS policy reference exists and states the tier mapping.
        self.assertTrue(policy.exists(), "references/cvss-policy.md missing")
        ptext = policy.read_text()
        self.assertIn("CVSS v4.0 Base", ptext)
        self.assertIn("9.0", ptext)  # P0 band
        # phase-3 scores with CVSS and no longer uses the D1-D4 rubric.
        self.assertIn("CVSS v4.0 Base", phase3)
        self.assertIn("references/cvss-policy.md", phase3)
        self.assertNotIn("## Severity Rubric", phase3)
        self.assertNotIn("Total = D1 + D2 + D3 + D4", phase3)
        # enums document CVSS (§18) and the tier is derived from the score.
        self.assertIn("CVSS v4.0 Base", enums)
        self.assertIn("cvss_score", enums)
        self.assertIn("cvss_vector", enums)

    def test_phase7_findings_json_carries_cvss_fields(self):
        phase7 = (ROOT / "phases" / "phase-7.md").read_text()
        self.assertIn("cvss_score", phase7)
        self.assertIn("cvss_vector", phase7)
        self.assertIn("full Base vector", phase7)


if __name__ == "__main__":
    unittest.main()
