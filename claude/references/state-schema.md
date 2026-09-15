# State Schema — state.json

## Full Schema

```json
{
  "date": "YYYY-MM-DD",
  "model": "<model-id or empty string>",
  "mode": "review | triage",
  "current_phase": "phase-0",
  "repo_slugs": ["slug-1", "slug-2"],
  "phase_status": {
    "<phase-name>": "completed"
  },
  "git_checkpoints": [
    {
      "phase": "<phase-name>",
      "checkpoint_uuid": "<UUIDv4>",
      "timestamp": "<ISO 8601>"
    }
  ],
  "agent_journal": {
    "schema_version": "1",
    "filename": "agent-conversation-YYYY-MM-DD.md",
    "session_id": "<UUIDv4>",
    "epoch_id": "<UUIDv4>",
    "epoch_phase": "phase-0",
    "epoch_status": "open",
    "accepted_sequence": 0,
    "accepted_length": 0,
    "accepted_head_sha256": null,
    "checkpoint_uuid": "<UUIDv4>"
  },
  "history": [
    {
      "phase": "<phase-name>",
      "completed_at": "<ISO 8601>"
    }
  ]
}
```

## Key Fields

### `current_phase`
The phase that should execute next. Set by `init-review.sh` to `phase-0`.
Advanced by `complete-phase.sh` after each phase completes.
Set to `"done"` when phase-8 completes.

### `model`
Advisory run-start provenance only. It may contain the label supplied through
the compatibility `--model <label>` argument, but it never selects, pins,
retries, or routes a worker. NightFalcon omits the Agent tool's `model`
parameter for every phase, Devil's Advocate, PoC reviewer, and retry. Each new
spawn therefore uses the model currently selected by the user in Claude Code;
already-running agents keep their launch model. An empty value means no model
label was recorded. Obsolete `phase_models` data is ignored.

### `mode`
The run mode, set once by `init-review.sh` and **immutable for the run**:
- `"review"` (default) — full discovery pipeline from cloned source
  (`phase-0` → `phase-7`).
- `"triage"` — ingest an external findings backlog and re-adjudicate it,
  starting at `triage-ingest` and skipping clone/scope/dataflow/scoring
  (`triage-ingest` → `phase-4` → `phase-7`).
`complete-phase.sh` selects the phase-order array from this field and
`check-gate.sh` keys its preconditions on it, so phase ordering is enforced
in code, not by the orchestrator. An absent `mode` is treated as `review`.

### `repo_slugs`
List of short identifiers for each repo being reviewed. Written by the orchestrator
after phase-0 clones complete. Used by `check-gate.sh` to verify per-repo output files.

### `phase_status`
Map of `phase-name → "completed"`. A phase absent from this map has not run.
Only `complete-phase.sh` may set a phase to completed — and only after verifying
all expected output files exist and are non-empty.

### `git_checkpoints`
Append-only list of git commits created by `complete-phase.sh` after each phase.
New entries use `checkpoint_uuid`; the engine resolves the corresponding
`refs/nightfalcon/checkpoints/<session_id>/<phase>` ref and requires exactly
one commit with a matching `NightFalcon-Checkpoint-ID: <uuid>` trailer.
Legacy entries containing `sha` may coexist and are preserved, but they do not
substitute for UUID/ref/trailer authority on new journal-managed checkpoints.

### `agent_journal`
Gate-owned v1 journal state. It binds the canonical journal filename and
session, the current phase epoch, the immutable accepted byte/hash prefix, and
the UUID reserved for the next checkpoint. Older workspaces without this
object are upgraded transactionally by `agent-journal.py init`; unknown state
fields and legacy checkpoint SHA entries are retained. An initialized state
must have a non-null canonical `date`.

### `history`
Append-only log of all phase completions with timestamps.

## Finding ID Scheme

Findings use stable IDs of the form `F-NNN` (zero-padded three-digit,
e.g. `F-001`, `F-042`), assigned at the moment a candidate is raised in
phase-3 and **never changed** for the lifetime of the finding. The same
`F-NNN` ID appears in the candidate record, debate transcript, findings
report, executive summary, and receipt artifact.

IDs are sequential **per repo** — each repo restarts numbering at
`F-001`. Tier (`P0` … `P4`) is conveyed by section headings and a
separate `tier_initial` / `tier_final` field in receipts, never embedded
in the ID itself. This makes `grep F-042` work uniformly across every
artifact a review produces.

The legacy `## CANDIDATE-<N>` block heading is retained as a per-finding
anchor required by `complete-phase.sh` for phase-3 verification. New
artifacts use `F-NNN` only.

## Phase Order

```
phase-0 → phase-1 → phase-2 → phase-3 → phase-4 → phase-5 → phase-6 → phase-7 → phase-8 → done
```

## Gate Enforcement

Before every phase:
```bash
bash scripts/check-gate.sh --next-phase <phase> --workspace <workspace>
# Exit 2 = BLOCKED — stop, do not spawn subagent
```

After every phase:
```bash
bash scripts/complete-phase.sh --phase <phase> --workspace <workspace> [options]
# Exit 2 = BLOCKED — phase output invalid, do not advance
```

## Gate is authoritative — do not substitute heuristics

The gate scripts (`check-gate.sh`, `complete-phase.sh`) are the **only
authoritative check** of phase completion. They combine three layers
of validation:

1. Existence + non-empty file check (`[[ -s "$f" ]]`).
2. Content-marker grep (e.g., `## Findings Summary`, `Final
   Disposition:`, `Validation:`).
3. Schema-level validation for JSON artifacts (receipt JSON, pattern-
   tags JSON, dataflow JSON) — required fields and parse-correctness.

Operator status loops, dashboard scripts, and ad-hoc shell snippets
**should not substitute** `wc -l`, `find -size +0`, or other file-size
heuristics for the gate's three layers. In particular: a 0-byte file
plus a `phase_status: completed` entry is impossible by gate
construction (layer 1 catches it), but an operator script that polls
file size in parallel with the gate may briefly observe one state or
the other in isolation. Such a script will produce confusing output —
"phase ran, produced nothing" — when the truth is "gate caught the
empty file and blocked the advance."

If you need a status dashboard, read `state.json.phase_status` and
the latest `git_checkpoints[]` entry. Those are gate-managed and
therefore authoritative. A 0-byte file on disk during an in-flight
phase is normal (some subagents `touch` before writing); the gate
runs to completion before flipping `phase_status` to `completed`.

## Special Cases

- **Zero candidates (phase-3):** Write `NO_CANDIDATES` marker to candidates file.
  Use `--skip-candidates` flag with complete-phase.sh.
  Skip phases 4 and 5: use `--no-debate-needed` and `--no-validation-needed`
  to advance state.json without requiring output files.

- **No P0/CRIT/HIGH (phase-4):** Use `--no-debate-needed`.

- **No eligible validation candidates (phase-5):** Use `--no-validation-needed`.
