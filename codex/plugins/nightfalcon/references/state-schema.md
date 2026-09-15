# State Schema — state.json

## Full Schema

```json
{
  "date": "YYYY-MM-DD",
  "model": "<initial advisory Codex model slug>",
  "model_policy": "user-selected",
  "model_provenance": "available",
  "model_history": [
    {
      "model": "<initial exact Codex model slug>",
      "session_id": "<initial session id>",
      "source": "SessionStart",
      "selected_at": "<ISO 8601>"
    }
  ],
  "plugin_version": "<installed plugin version>",
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
      "timestamp": "<ISO 8601>",
      "journal_transition_version": "2"
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

This representative schema shows the state created at initialization. Its
model fields are initial provenance only. Later user selections are recorded
externally under `$PLUGIN_DATA/model-selections/`; they do not rewrite run
state or establish an expected runtime model.

When the optional SessionStart pin is missing or unusable, newly initialized
state instead records `"model": "unknown"`,
`"model_provenance": "unavailable"`, and `"model_history": []`. The session
manifest records the same model and provenance availability. Initialization
continues because provenance is best-effort.

## Key Fields

### `current_phase`
The phase that should execute next. Set by `init-review.sh` to `phase-0`.
Advanced by `complete-phase.sh` after each phase completes.
Set to `"done"` when phase-8 completes.

### `model`
The advisory model reported by Codex when the run was initialized, or
`"unknown"` when no usable SessionStart pin was available. It is initial
provenance only, not a current selection, expected model, or gate. SessionStart
may report it with `NIGHTFALCON_MODEL_SELECTED`; `init-review.sh` consumes a
usable pin only as optional best-effort provenance.

NightFalcon never selects or switches models. Omit the `model` argument and
every reasoning override so Codex uses the model currently selected by the
user. A user may change that selection at any time. Already-running agents
keep their launch model; later turns and new agents use the new selection.
Never reject, retry, or reroute work because the observed model differs.

### `model_policy`
Newly initialized state records `"user-selected"`. This documents that Codex,
under user control, chooses each turn or agent's model and NightFalcon does not
enforce identity. Preserved legacy state may omit `model_policy` or retain
obsolete model-control fields; resume leaves that state byte-for-byte unchanged.

### `model_history`
Contains one entry when a valid SessionStart selection initialized the run, or
is empty when provenance was unavailable. It is initial provenance only and
has no epoch. Passive later selection events are external to the workspace
under `$PLUGIN_DATA/model-selections/` so model changes never mutate review
state.

Existing workspaces may contain legacy `model_lock`, `model_epoch`, or
additional model-history fields. Legacy state may omit `model_policy` entirely.
These states are ignored and left untouched for compatibility; current runtime
code neither consults nor updates their model metadata.

After a completed Phase-6 pass, the orchestrator may retry refused findings
once with `RETRY_FINDINGS`, using the model selected by the user when the new
worker launches.
Spawn or wait failure blocks the phase. Phase-6 retry state does not carry
forward.

### `mode`
The run mode, set once by `init-review.sh` and **immutable for the run**:
- `"review"` (default) — full discovery pipeline from cloned source
  (`phase-0` → `phase-1` → `phase-2` → `phase-3` → `phase-4` → `phase-5` → `phase-6` → `phase-7` → `phase-8`).
- `"triage"` — ingest an external findings backlog and re-adjudicate it,
  starting at `triage-ingest` and skipping clone/scope/dataflow/scoring
  (`triage-ingest` → `phase-4` → `phase-5` → `phase-6` → `phase-7` → `phase-8`).
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
Codex transaction-v2 entries also carry `journal_transition_version: "2"`.
For every nonterminal acceptance, same durable transaction rotates epoch and
adds exactly one canonical `user-intent-recorded` event for next phase. This
lets one orchestrator invocation continue without a separate resume step.
Legacy entries containing `sha` may coexist and are preserved, but they do not
substitute for UUID/ref/trailer authority on new journal-managed checkpoints;
entries without transition marker retain v1 sequence semantics during resume.

Resume-driven epoch rotation uses a separate durable exact-prefix transaction.
It binds exact old/new state bytes and the complete old-epoch recovery sequence,
`run-resumed`, and `user-intent-recorded` suffix bytes before mutation.
Initialization recognizes
a pending resume transaction without parsing a torn suffix; reconciliation
then completes any exact partial write, converges state, validates one canonical
current-epoch intent, and removes the transaction.
If an epoch still contains open agent attempts, default reconciliation fails
closed. Every attempt requires an exact terminal journal event recorded through
normal mode-specific lifecycle handling after actual runtime cleanup. An empty
agent list in a different root task is not proof, and reconciliation never
synthesizes terminal evidence.

Fresh initialization with authoritative repository slugs atomically writes
`run-initialized` and one canonical `user-intent-recorded` event in the
initialization transaction. Legacy phase-0 state without slugs retains only
`run-initialized` until repository scope becomes authoritative. Nonterminal
v2 acceptance rotates into an open next-phase epoch with one canonical intent;
terminal v2 acceptance instead retains and closes the phase-8 epoch.

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
  `--skip-candidates` may verify an all-empty Phase-3 run, but Phase 4 still
  starts one worker for every slug.

- **No P0/P1/P2 (phase-4):** The repo's worker writes a
  `NO_DEBATE_CANDIDATES` debate artifact with the canonical gate sentinel.

- **No eligible validation candidates (phase-5):** The repo's worker appends
  a `NO_VALIDATION_CANDIDATES` / `Validation: NOT-RUN` block.

- **No eligible PoC candidates (phase-6):** The repo's worker writes an empty
  schema-v3 PoC manifest.

The gate scripts retain `--no-debate-needed`, `--no-validation-needed`, and
`--no-poc-needed` as operator recovery utilities. Normal orchestration does
not use them because Phases 4–6 always run one worker per repo slug and require
per-repo no-work artifacts.
