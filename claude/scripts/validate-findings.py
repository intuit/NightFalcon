#!/usr/bin/env python3
"""Validate NightFalcon cross-phase finding provenance contracts."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import shlex
import subprocess
import sys

from cvss_v4 import CVSS4Error, score_base_vector, tier_for_score

CALCULATOR = "https://www.first.org/cvss/calculator/4.0#"
EXPOSURES = {"EXTERNAL", "INTERNAL", "INTERNAL-RESTRICTED"}
DISPOSITIONS = {"CONFIRMED", "CONFIRMED-MODIFIED", "DISMISSED", "NEEDS-REVIEW"}
VALIDATIONS = {"CURRENT", "ACTIVELY-EXPLOITED", "PATCHED", "UNVERIFIED", "NOT-RUN"}
POC_VERDICTS = {"VALID", "NEEDS-REVISION", "INVALID"}
POC_TYPES = {"curl-shell", "python", "browser-recipe", "source-check", "other"}
CONFIRM_PATHS = {"script", "browser", "unconfirmed"}
PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_]*")
REVIEWER_FIELDS = {
    "reviewer", "verdict", "reasoning", "accuracy_ok", "safety_ok",
    "completeness_ok", "suggested_fix",
}
FAILURE_SKIP_REASONS = {"generation-refused", "generation-failed", "agent-budget-exhausted"}
SKIP_REASONS = FAILURE_SKIP_REASONS | {
    "not-eligible-tier", "not-eligible-disposition", "no-candidates",
}
REQUIRED_MD = (
    "what_happens_md", "attack_steps_md", "evidence_md", "fix_md", "severity_md",
)
REQUIRED_REPORT_TOP_LEVEL = (
    "dismissed_findings", "poc_coverage", "methodology_notes", "disclaimer",
)
ALLOWED_REPORT_TOP_LEVEL = {
    "schema_version", "repo_slug", "repo_url", "commit_sha", "multitenant_scope",
    "date", "findings", *REQUIRED_REPORT_TOP_LEVEL,
}
REQUIRED_STRUCTURED = (
    "exposure", "reachability", "exploitability", "impact", "patch_status",
)
REQUIRED_FINDING_TEXT = (
    "finding_id", "tier", "cvss_vector", "title", "category", "location",
    "validation", *REQUIRED_MD, *REQUIRED_STRUCTURED,
)
ALLOWED_FINDING_FIELDS = {
    *REQUIRED_FINDING_TEXT, "cvss_score", "cwe", "cve", "poc", "references",
    "controls_in_scope", "applicable_policies",
}
CONTROL_FIELDS = {"name", "control_id", "note_path"}
POLICY_FIELDS = {"id", "title", "binding_statement", "note_path"}


def fail(message: str) -> None:
    raise ValueError(message)


def load_object(path: pathlib.Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        fail(f"{label} missing: {path}")
    except json.JSONDecodeError as exc:
        fail(f"{label} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def _markdown_values(record: str, label: str) -> list[str]:
    pattern = re.compile(
        rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(label)}:"
        rf"(?:\*\*)?\s*(.*?)\s*$"
    )
    return [value.rstrip("*").strip() for value in pattern.findall(record)]


def _markdown_value(record: str, label: str) -> str:
    values = _markdown_values(record, label)
    if len(values) != 1 or not values[0]:
        fail(f"candidate must contain exactly one non-empty {label} field")
    return values[0]


def _final_tier(record: str) -> str:
    values = _markdown_values(record, "Final Tier") + _markdown_values(
        record, "Final Tier (post-debate)"
    )
    if len(values) != 1 or not values[0]:
        fail("candidate must contain exactly one non-empty Final Tier field")
    return values[0].split()[0]


def _validate_scoring(record: str, prefix: str, candidate_id: str) -> tuple[float, str, str]:
    score_label = f"{prefix}CVSS-B Score"
    vector_label = f"{prefix}CVSS Vector"
    rationale_label = f"{prefix}CVSS Rationale"
    calculator_label = f"{prefix}CVSS Calculator"
    tier_label = "Tier" if not prefix else "Final Tier"
    score_text = _markdown_value(record, score_label)
    vector = _markdown_value(record, vector_label).strip(chr(96))
    _markdown_value(record, rationale_label)
    calculator = _markdown_value(record, calculator_label)
    tier = _markdown_value(record, tier_label).split()[0] if not prefix else _final_tier(record)
    try:
        claimed = float(score_text)
    except ValueError:
        fail(f"candidate {candidate_id}: {score_label} is not numeric: {score_text!r}")
    try:
        computed = score_base_vector(vector)
    except CVSS4Error as exc:
        fail(f"candidate {candidate_id}: invalid {vector_label}: {exc}")
    if claimed != computed:
        fail(
            f"candidate {candidate_id}: {score_label} {claimed:.1f} does not match "
            f"vector score {computed:.1f}"
        )
    expected_tier = tier_for_score(computed)
    if tier != expected_tier:
        fail(
            f"candidate {candidate_id}: {tier_label} {tier!r} does not match "
            f"CVSS score {computed:.1f} ({expected_tier})"
        )
    expected_calculator = CALCULATOR + vector
    if calculator != expected_calculator:
        fail(f"candidate {candidate_id}: {calculator_label} must be {expected_calculator}")
    return computed, vector, tier


def validate_dataflow(path: pathlib.Path, *, slug: str, date: str) -> dict[str, str]:
    document = load_object(path, "dataflow JSON")
    if document.get("schema_version") != "1":
        fail('dataflow schema_version must be exactly "1"')
    if document.get("repo_slug") != slug:
        fail(f"dataflow repo_slug must match state slug {slug!r}")
    if document.get("date") != date:
        fail(f"dataflow date must match state date {date!r}")
    flows = document.get("flows")
    if not isinstance(flows, list):
        fail("dataflow flows must be a list")
    relationships = document.get("relationship_context")
    invariants = document.get("business_logic_invariants")
    outbound_edges = document.get("outbound_edges")
    if not isinstance(relationships, list):
        fail("dataflow relationship_context must be a list")
    if not isinstance(invariants, list):
        fail("dataflow business_logic_invariants must be a list")
    if not isinstance(outbound_edges, list):
        fail("dataflow outbound_edges must be a list")
    coverage = document.get("analysis_coverage")
    if not isinstance(coverage, dict):
        fail("dataflow analysis_coverage must be an object")
    for dimension, records in (
        ("authorization", relationships),
        ("business_logic", invariants),
        ("cross_repository", outbound_edges),
    ):
        entry = coverage.get(dimension)
        if not isinstance(entry, dict):
            fail(f"analysis_coverage.{dimension} must be an object")
        expected_status = "analyzed" if records else "not-applicable"
        if entry.get("status") != expected_status:
            fail(f"analysis_coverage.{dimension}.status must be {expected_status!r}")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or len(rationale.strip()) < 20:
            fail(f"analysis_coverage.{dimension}.rationale must be evidence-based")
        flow_ids = entry.get("flow_ids")
        if not isinstance(flow_ids, list) or not all(isinstance(value, str) for value in flow_ids):
            fail(f"analysis_coverage.{dimension}.flow_ids must be a string list")
    for index, item in enumerate(relationships):
        if not isinstance(item, dict):
            fail(f"relationship_context at index {index} must be an object")
        required = {"flow_id", "case_id", "principal", "action", "object", "relationship", "selector_substitution", "observed_decision", "expected_decision", "enforcement_points", "evidence"}
        if not required <= set(item):
            fail(f"relationship_context at index {index} is missing required fields")
        if item["relationship"] not in {"owner", "same-tenant-nonowner", "cross-tenant", "delegated", "admin", "anonymous", "unknown"}:
            fail(f"relationship_context at index {index} has invalid relationship")
        if not isinstance(item["principal"], dict) or not isinstance(item["object"], dict):
            fail(f"relationship_context at index {index} principal/object must be objects")
        selector_provenance = item["object"].get("selector_provenance")
        if not isinstance(selector_provenance, str) or not selector_provenance.strip():
            fail(f"relationship_context at index {index} object.selector_provenance must be non-empty")
        substitution = item["selector_substitution"]
        if not isinstance(substitution, dict) or not all(
            isinstance(substitution.get(field), str) and substitution[field].strip()
            for field in ("source", "result")
        ):
            fail(f"relationship_context at index {index} selector_substitution must contain source and result")
        if not isinstance(item["action"], str) or not item["action"].strip():
            fail(f"relationship_context at index {index} action must be non-empty")
        if item["observed_decision"] not in {"allow", "deny", "conditional", "unknown"} or item["expected_decision"] not in {"allow", "deny", "conditional", "unknown"}:
            fail(f"relationship_context at index {index} has invalid decision")
        if not isinstance(item["enforcement_points"], list) or not all(isinstance(value, str) for value in item["enforcement_points"]):
            fail(f"relationship_context at index {index} enforcement_points must be a string list")
        if not isinstance(item["evidence"], list) or not item["evidence"]:
            fail(f"relationship_context at index {index} requires evidence")
    for index, item in enumerate(invariants):
        if not isinstance(item, dict):
            fail(f"business_logic_invariants at index {index} must be an object")
        required = {"flow_id", "invariant", "key_scope", "bounds", "read_check_write", "atomicity", "idempotency", "actors", "interleavings", "state_transitions", "rollback", "retries", "replay", "quotas", "evidence"}
        if not required <= set(item):
            fail(f"business_logic_invariants at index {index} is missing required fields")
        if not isinstance(item["evidence"], list) or not item["evidence"]:
            fail(f"business_logic_invariants at index {index} requires evidence")
        if item["key_scope"] not in {"principal", "tenant", "account", "object", "global", "composite", "unknown"}:
            fail(f"business_logic_invariants at index {index} has invalid key_scope")
        for field in ("bounds", "atomicity", "idempotency", "rollback", "retries", "replay", "quotas"):
            if not isinstance(item[field], dict):
                fail(f"business_logic_invariants at index {index} {field} must be an object")
        for field in ("read_check_write", "actors", "interleavings", "state_transitions"):
            if not isinstance(item[field], list):
                fail(f"business_logic_invariants at index {index} {field} must be a list")
    for index, edge in enumerate(outbound_edges):
        if not isinstance(edge, dict) or not isinstance(edge.get("target"), str) or not edge["target"]:
            fail(f"outbound_edges at index {index} requires a non-empty target")
        if not isinstance(edge.get("evidence"), list) or not edge["evidence"]:
            fail(f"outbound_edges at index {index} requires evidence")
        if not isinstance(edge.get("flow_id"), str):
            fail(f"outbound_edges at index {index} requires flow_id")
    by_id: dict[str, str] = {}
    for index, flow in enumerate(flows):
        if not isinstance(flow, dict):
            fail(f"dataflow flow at index {index} must be an object")
        flow_id = flow.get("flow_id")
        if not isinstance(flow_id, str) or re.fullmatch(r"Flow-[1-9][0-9]*", flow_id) is None:
            fail(f"dataflow flow at index {index}: flow_id must match Flow-N")
        if flow_id in by_id:
            fail(f"dataflow contains duplicate flow_id {flow_id}")
        exposure = flow.get("exposure")
        if exposure not in EXPOSURES:
            fail(
                f"dataflow {flow_id}: exposure must be one of "
                f"{', '.join(sorted(EXPOSURES))}"
            )
        by_id[flow_id] = exposure
    known = set(by_id)
    for dimension in ("authorization", "business_logic", "cross_repository"):
        entry = coverage[dimension]
        referenced = set(entry["flow_ids"])
        if not referenced <= known:
            fail(f"analysis_coverage.{dimension}.flow_ids references unknown flows")
        if entry["status"] == "analyzed" and not referenced:
            fail(f"analysis_coverage.{dimension}.flow_ids cannot be empty when analyzed")
    for label, records in (
        ("relationship_context", relationships),
        ("business_logic_invariants", invariants),
        ("outbound_edges", outbound_edges),
    ):
        for index, record in enumerate(records):
            if record.get("flow_id") not in known:
                fail(f"{label} at index {index} references unknown flow_id")
    return by_id


def parse_candidates(
    path: pathlib.Path,
    *,
    post_debate: bool,
    require_flow: bool,
    require_validation: bool = False,
) -> dict[str, dict]:
    text = path.read_text()
    headings = list(re.finditer(r"(?m)^## CANDIDATE-[^\n]*", text))
    if not headings:
        if "NO_CANDIDATES" in text:
            return {}
        fail("candidate file has no CANDIDATE sections or NO_CANDIDATES marker")
    if "NO_CANDIDATES" in text:
        fail("NO_CANDIDATES cannot coexist with candidate sections")
    candidates: dict[str, dict] = {}
    for index, heading in enumerate(headings):
        ids = re.findall(r"F-[0-9]{3}", heading.group(0))
        if len(ids) != 1:
            fail(f"candidate heading {index + 1} must contain exactly one F-NNN finding ID")
        finding_id = ids[0]
        if finding_id in candidates:
            fail(f"candidate file contains duplicate finding_id {finding_id}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        record = text[heading.end():end]
        score, vector, tier = _validate_scoring(record, "", finding_id)
        exposure = _markdown_value(record, "Exposure")
        if exposure not in EXPOSURES:
            fail(
                f"candidate {finding_id}: exposure must be one of "
                f"{', '.join(sorted(EXPOSURES))}"
            )
        candidate = {
            "finding_id": finding_id,
            "initial_score": score,
            "initial_vector": vector,
            "initial_tier": tier,
            "exposure": exposure,
        }
        flow_values = _markdown_values(record, "Flow")
        if len(flow_values) > 1:
            fail(f"candidate {finding_id}: Flow must appear at most once")
        if flow_values:
            flow_id = flow_values[0]
            if re.fullmatch(r"Flow-[1-9][0-9]*", flow_id) is None:
                fail(f"candidate {finding_id}: Flow must be exactly one Flow-N value")
            candidate["flow_id"] = flow_id
        elif require_flow:
            fail(f"candidate {finding_id}: Flow must reference exactly one Flow-N")
        if post_debate:
            dispositions = _markdown_values(record, "Final Disposition")
            if len(dispositions) != 1 or dispositions[0] not in DISPOSITIONS:
                fail(
                    f"candidate {finding_id}: Final Disposition must appear exactly once "
                    "and use the canonical enum"
                )
            final_score, final_vector, final_tier = _validate_scoring(
                record, "Final ", finding_id
            )
            candidate.update(
                disposition=dispositions[0],
                final_score=final_score,
                final_vector=final_vector,
                final_tier=final_tier,
            )
            dismissed_titles = _markdown_values(record, "Dismissed Finding Title")
            dismissal_reasons = _markdown_values(record, "Dismissal Reason")
            if dispositions[0] == "DISMISSED":
                if len(dismissed_titles) != 1 or len(dismissal_reasons) != 1:
                    fail(
                        f"candidate {finding_id}: DISMISSED requires exactly one "
                        "Dismissed Finding Title and Dismissal Reason"
                    )
                candidate["dismissed_title"] = dismissed_titles[0]
                candidate["dismissal_reason"] = dismissal_reasons[0]
            elif dismissed_titles or dismissal_reasons:
                fail(
                    f"candidate {finding_id}: dismissal metadata is allowed only for "
                    "Final Disposition DISMISSED"
                )
            validation_values = _markdown_values(record, "Validation")
            if len(validation_values) > 1:
                fail(
                    f"candidate {finding_id}: Validation must appear at most once "
                    "inside its candidate record"
                )
            if validation_values and validation_values[0] not in VALIDATIONS:
                fail(f"candidate {finding_id}: Validation must use the canonical enum")
            eligible = (
                candidate["final_tier"] in {"P0", "P1", "P2"}
                and candidate["disposition"] != "DISMISSED"
            )
            if require_validation and eligible and len(validation_values) != 1:
                fail(
                    f"candidate {finding_id}: eligible candidate requires exactly "
                    "one record-level Validation result"
                )
            if (
                require_validation
                and validation_values
                and not eligible
                and validation_values[0] != "NOT-RUN"
            ):
                fail(
                    f"candidate {finding_id}: ineligible or DISMISSED candidate "
                    "Validation must be NOT-RUN"
                )
            if validation_values:
                candidate["validation"] = validation_values[0]
            elif require_validation and not eligible:
                candidate["validation"] = "NOT-RUN"
        candidates[finding_id] = candidate
    return candidates


def validate_candidates(
    path: pathlib.Path,
    *,
    dataflow_path: pathlib.Path | None,
    post_debate: bool,
    slug: str | None,
    date: str | None,
    require_validation: bool = False,
) -> dict[str, dict]:
    candidates = parse_candidates(
        path,
        post_debate=post_debate,
        require_flow=dataflow_path is not None,
        require_validation=require_validation,
    )
    if dataflow_path is not None:
        if not slug or not date:
            fail("--slug and --date are required with --dataflow")
        flows = validate_dataflow(dataflow_path, slug=slug, date=date)
        for finding_id, candidate in candidates.items():
            flow_id = candidate["flow_id"]
            if flow_id not in flows:
                fail(f"candidate {finding_id}: Flow {flow_id} does not exist in dataflow JSON")
            if candidate["exposure"] != flows[flow_id]:
                fail(
                    f"candidate {finding_id}: exposure {candidate['exposure']} does not "
                    f"match {flow_id} exposure {flows[flow_id]}"
                )
    return candidates


def _require_nonempty_string(document: dict, key: str, label: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: missing or empty field: {key}")
    return value.strip()


def _strip_hash_comments(content: str) -> str:
    """Remove shell/Python-style comments while preserving quoted # characters."""
    cleaned: list[str] = []
    for line in content.splitlines():
        quote: str | None = None
        escaped = False
        end = len(line)
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote != "'":
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == "#" and (index == 0 or line[index - 1].isspace()):
                end = index
                break
        cleaned.append(line[:end])
    return "\n".join(cleaned)


def _manifest_workspace(path: pathlib.Path, slug: str) -> tuple[pathlib.Path, pathlib.Path]:
    resolved = path.resolve()
    if len(resolved.parents) < 4:
        fail("PoC manifest path is not under a review workspace")
    workspace = resolved.parents[3]
    expected_dir = (workspace / "output" / "proof_of_concept" / slug).resolve()
    if resolved.parent != expected_dir:
        fail(
            f"PoC manifest must be under output/proof_of_concept/{slug}/"
        )
    return workspace, expected_dir


def _validate_reviewer_envelopes(poc: dict, finding_id: str) -> None:
    regenerated = poc.get("regenerated")
    if not isinstance(regenerated, bool):
        fail(f"poc {finding_id}: regenerated must be a boolean")
    envelopes = poc.get("reviewer_envelopes")
    expected_count = 4 if regenerated else 2
    if not isinstance(envelopes, list) or len(envelopes) != expected_count:
        fail(
            f"poc {finding_id}: reviewer_envelopes must contain exactly "
            f"{expected_count} entries"
        )
    expected_labels = ["A", "B"] * (2 if regenerated else 1)
    for index, (envelope, expected_label) in enumerate(
        zip(envelopes, expected_labels, strict=True)
    ):
        label = f"poc {finding_id}: reviewer_envelopes[{index}]"
        if not isinstance(envelope, dict) or set(envelope) != REVIEWER_FIELDS:
            fail(
                f"{label} must contain exactly reviewer, verdict, reasoning, "
                "accuracy_ok, safety_ok, completeness_ok, suggested_fix"
            )
        if envelope["reviewer"] != expected_label:
            fail(f"{label}: reviewer must be {expected_label}")
        if envelope["verdict"] not in POC_VERDICTS:
            fail(f"{label}: verdict must use the canonical enum")
        _require_nonempty_string(envelope, "reasoning", label)
        for key in ("accuracy_ok", "safety_ok", "completeness_ok"):
            if not isinstance(envelope[key], bool):
                fail(f"{label}: {key} must be a boolean")
        suggested = envelope["suggested_fix"]
        if suggested is not None and (
            not isinstance(suggested, str) or not suggested.strip()
        ):
            fail(f"{label}: suggested_fix must be null or a non-empty string")
        flags = [
            envelope["accuracy_ok"],
            envelope["safety_ok"],
            envelope["completeness_ok"],
        ]
        if envelope["verdict"] == "VALID" and (
            not all(flags) or suggested is not None
        ):
            fail(
                f"{label}: VALID requires all reviewer checks true and "
                "suggested_fix null"
            )
        if envelope["verdict"] == "NEEDS-REVISION" and (
            all(flags) or suggested is None
        ):
            fail(
                f"{label}: NEEDS-REVISION requires at least one failed reviewer "
                "check and a concrete suggested_fix"
            )
        if envelope["verdict"] == "INVALID" and (
            envelope["accuracy_ok"] or suggested is None
        ):
            fail(
                f"{label}: INVALID requires accuracy_ok false and a concrete "
                "suggested_fix"
            )
    latest = [entry["verdict"] for entry in envelopes[-2:]]
    if regenerated:
        initial = [entry["verdict"] for entry in envelopes[:2]]
        if "INVALID" in initial or "NEEDS-REVISION" not in initial:
            fail(
                f"poc {finding_id}: regeneration requires an initial reviewer pair "
                "with NEEDS-REVISION and no INVALID verdict"
            )
    resolved = (
        "INVALID"
        if "INVALID" in latest
        else "VALID"
        if latest == ["VALID", "VALID"]
        else "NEEDS-REVISION"
    )
    if poc.get("verdict") != resolved:
        fail(
            f"poc {finding_id}: verdict does not match the final reviewer envelope pair"
        )
    if not regenerated and resolved == "NEEDS-REVISION":
        fail(f"poc {finding_id}: NEEDS-REVISION requires one regeneration round")


def _require_poc_header(header: object, finding_id: str) -> None:
    if not isinstance(header, str) or not header.strip():
        fail(f"poc {finding_id}: script requires a non-empty safety header")
    if "SAFETY:" not in header or "self-contained" not in header.lower():
        fail(
            f"poc {finding_id}: safety header must state that the PoC is "
            "self-contained"
        )
    if "Expected observation on success:" not in header:
        fail(f"poc {finding_id}: header requires Expected observation on success")


def _shell_commands(content: str, finding_id: str) -> list[list[str]]:
    logical = content.replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(logical, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        fail(f"poc {finding_id}: shell tokenization failed: {exc}")
    commands: list[list[str]] = []
    command: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if command:
                commands.append(command)
                command = []
        else:
            command.append(token)
    if command:
        commands.append(command)
    return commands


def _validate_shell_output_wiring(
    active: str, finding_id: str, commands: list[list[str]]
) -> None:
    def under_output_dir(target: str) -> bool:
        match = re.match(
            r"^(?:\$POC_OUTPUT_DIR|\$\{POC_OUTPUT_DIR(?:%/)?\})(?P<suffix>/.*)?$",
            target,
        )
        return match is not None and ".." not in pathlib.PurePosixPath(
            match.group("suffix") or ""
        ).parts

    def control_target(target: str) -> bool:
        return target == "-" or re.match(
            r"^/dev/(?:null|stdout|stderr|fd/[0-9]+)$", target
        ) is not None

    def safe_relative(target: str) -> bool:
        relative = pathlib.PurePosixPath(target)
        return not relative.is_absolute() and ".." not in relative.parts

    def require_contained(
        target: str, sink: str, *, contained_base: str | None = None
    ) -> bool:
        if control_target(target):
            return False
        if under_output_dir(target) or (
            contained_base is not None
            and under_output_dir(contained_base)
            and safe_relative(target)
        ):
            return True
        else:
            fail(
                f"poc {finding_id}: {sink} target {target!r} must be under "
                "POC_OUTPUT_DIR"
            )

    def executable(command: list[str]) -> tuple[str | None, list[str]]:
        index = 0
        assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
        while index < len(command) and (
            command[index] in {"!", "if", "then", "do", "elif", "else"}
            or assignment.match(command[index])
        ):
            index += 1
        if index >= len(command) or command[index] in {"[[", "((", "fi", "done"}:
            return None, []
        program = pathlib.PurePosixPath(command[index]).name
        args = command[index + 1 :]
        for _depth in range(16):
            if program == "command":
                index = 0
                while index < len(args) and args[index].startswith("-"):
                    token = args[index]
                    if token == "--":
                        index += 1
                        break
                    flags = token[1:]
                    if not flags or not set(flags) <= {"p", "v", "V"}:
                        fail(
                            f"poc {finding_id}: unsupported command wrapper option "
                            f"{token!r}"
                        )
                    if "v" in flags or "V" in flags:
                        return None, []
                    index += 1
                if index >= len(args):
                    return None, []
                program = pathlib.PurePosixPath(args[index]).name
                args = args[index + 1 :]
                continue
            if program == "env":
                index = 0
                while index < len(args):
                    token = args[index]
                    if token == "--":
                        index += 1
                        break
                    if assignment.match(token):
                        index += 1
                        continue
                    if token in {"-", "-i", "--ignore-environment"}:
                        index += 1
                        continue
                    if token in {"-u", "--unset", "-C", "--chdir"}:
                        if index + 1 >= len(args):
                            fail(
                                f"poc {finding_id}: incomplete env wrapper option "
                                f"{token!r}"
                            )
                        index += 2
                        continue
                    if token.startswith(("-u", "-C")) and len(token) > 2:
                        index += 1
                        continue
                    if token.startswith(("--unset=", "--chdir=")):
                        index += 1
                        continue
                    if token.startswith("-"):
                        fail(
                            f"poc {finding_id}: unsupported env wrapper option "
                            f"{token!r}"
                        )
                    break
                while index < len(args) and assignment.match(args[index]):
                    index += 1
                if index >= len(args):
                    return None, []
                program = pathlib.PurePosixPath(args[index]).name
                args = args[index + 1 :]
                continue
            return program, args
        fail(f"poc {finding_id}: too many nested command/env wrappers")

    def operands(args: list[str], value_options: set[str]) -> list[str]:
        values = []
        index = 0
        while index < len(args):
            token = args[index]
            if token in {">", ">>", "&>", "&>>", ">&", "<", "<<", "<&"}:
                index += 2
            elif token == "--":
                values.extend(args[index + 1 :])
                break
            elif token in value_options:
                index += 2
            elif token.startswith("-"):
                index += 1
            else:
                values.append(token)
                index += 1
        return values

    nested_writers = (
        "touch", "cp", "mv", "install", "truncate", "mkdir", "rm", "rmdir",
        "unlink", "ln", "tee", "dd", "tar", "unzip", "curl", "wget", "eval",
        "bash", "dash", "ksh", "sh", "zsh", "python", "python3", "perl",
        "ruby", "node", "nodejs", "php",
    )
    nested_pattern = "|".join(re.escape(program) for program in nested_writers)
    if "`" in active or re.search(
        rf"\$\([^)]*\b(?:{nested_pattern})\b", active
    ):
        fail(f"poc {finding_id}: unsupported nested execution may write artifacts")
    collapsed = active
    innermost = re.compile(r"\$\(([^()\n]*)\)")
    while match := innermost.search(collapsed):
        nested_commands = _shell_commands(match.group(1), finding_id)
        for nested in nested_commands:
            for index, token in enumerate(nested[:-1]):
                if token not in {">", ">>", "&>", "&>>", ">&"}:
                    continue
                target = nested[index + 1]
                if not control_target(target) and not (
                    token == ">&" and target.isdigit()
                ):
                    fail(
                        f"poc {finding_id}: command substitution may not write artifacts"
                    )
        collapsed = collapsed[: match.start()] + "SUBSTITUTION" + collapsed[match.end() :]

    if re.search(
        r'''(?m)^\s*POC_OUTPUT_DIR\s*=\s*["']?\$\{POC_OUTPUT_DIR:-[^}\n]+\}''',
        active,
    ) is None:
        fail(f"poc {finding_id}: script must assign a local POC_OUTPUT_DIR default")
    mkdir_ok = False
    output_ok = False
    for command in commands:
        for index, token in enumerate(command[:-1]):
            if token in {">", ">>", "&>", "&>>"}:
                in_test_expression = any(
                    opener in command[:index]
                    and closer in command[index + 1 :]
                    and command.index(opener) < index
                    for opener, closer in (("[[", "]]"), ("((", "))"))
                )
                if in_test_expression:
                    continue
                output_ok |= require_contained(command[index + 1], "redirect")
        program, args = executable(command)
        if program is None:
            continue
        if program in {"bash", "dash", "ksh", "sh", "zsh"} and any(
            token == "-c" or (token.startswith("-") and "c" in token[1:])
            for token in args
        ):
            fail(f"poc {finding_id}: unsupported nested shell execution")
        if program == "eval":
            fail(f"poc {finding_id}: eval is not allowed in a PoC script")
        interpreter_flags = {
            "node": {"-e", "--eval"}, "nodejs": {"-e", "--eval"},
            "perl": {"-e"}, "php": {"-r"}, "python": {"-c"},
            "python3": {"-c"}, "ruby": {"-e"},
        }
        if program in interpreter_flags and any(
            token in interpreter_flags[program] for token in args
        ):
            fail(f"poc {finding_id}: unsupported nested interpreter execution")
        if program == "xargs" or (
            program == "find"
            and any(token in {"-exec", "-execdir", "-ok", "-okdir", "-delete"} for token in args)
        ):
            fail(f"poc {finding_id}: unsupported nested command execution")
        if program == "tee":
            for token in args:
                if token in {">", ">>", ">&", "<", "<<", "<&"}:
                    break
                if not token.startswith("-"):
                    output_ok |= require_contained(token, "tee")
        if program == "curl":
            output_dir = None
            remote_name = False
            index = 0
            while index < len(args):
                token = args[index]
                if token == "--output-dir" and index + 1 < len(args):
                    output_dir = args[index + 1]
                    require_contained(output_dir, "curl --output-dir")
                    index += 2
                    continue
                if token.startswith("--output-dir="):
                    output_dir = token.split("=", 1)[1]
                    require_contained(output_dir, "curl --output-dir")
                index += 1
            index = 0
            while index < len(args):
                token = args[index]
                if token in {"-o", "--output"} and index + 1 < len(args):
                    output_ok |= require_contained(
                        args[index + 1], "curl output", contained_base=output_dir
                    )
                    index += 2
                    continue
                if token.startswith("--output="):
                    output_ok |= require_contained(
                        token.split("=", 1)[1], "curl output",
                        contained_base=output_dir,
                    )
                elif token in {
                    "-O", "--remote-name", "--remote-name-all",
                    "--remote-header-name",
                }:
                    remote_name = True
                elif token.startswith("-") and not token.startswith("--"):
                    flags = token[1:]
                    if "O" in flags:
                        remote_name = True
                    if "o" in flags:
                        attached = flags.split("o", 1)[1]
                        if attached:
                            target = attached
                        elif index + 1 < len(args):
                            target = args[index + 1]
                            index += 1
                        else:
                            fail(f"poc {finding_id}: curl -o requires a target")
                        output_ok |= require_contained(
                            target, "curl output", contained_base=output_dir
                        )
                index += 1
            if remote_name:
                if output_dir is None:
                    fail(
                        f"poc {finding_id}: curl remote-name output requires "
                        "--output-dir under POC_OUTPUT_DIR"
                    )
                output_ok = True
        if program == "wget":
            writer_found = False
            for index, token in enumerate(command[1:], start=1):
                if token in {"-O", "--output-document"} and index + 1 < len(command):
                    output_ok |= require_contained(command[index + 1], "wget output")
                    writer_found = True
                elif token.startswith("--output-document="):
                    output_ok |= require_contained(
                        token.split("=", 1)[1], "wget output"
                    )
                    writer_found = True
                elif token.startswith("-O") and token != "-O":
                    output_ok |= require_contained(token[2:], "wget output")
                    writer_found = True
            if not writer_found:
                fail(
                    f"poc {finding_id}: wget must use an output target under "
                    "POC_OUTPUT_DIR or '-' for stdout"
                )
        if program == "dd":
            for token in args:
                if token.startswith("of="):
                    output_ok |= require_contained(token.split("=", 1)[1], "dd")
        if program in {"rm", "rmdir", "unlink", "ln"}:
            fail(f"poc {finding_id}: destructive filesystem command {program} is forbidden")
        if program == "touch":
            for target in operands(args, {"-d", "--date", "-r", "--reference", "-t"}):
                output_ok |= require_contained(target, "touch")
        if program in {"cp", "mv", "install"}:
            target_dir = None
            for index, token in enumerate(args[:-1]):
                if token in {"-t", "--target-directory"}:
                    target_dir = args[index + 1]
                elif token.startswith("--target-directory="):
                    target_dir = token.split("=", 1)[1]
            values = operands(
                args,
                {"-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g", "--group"},
            )
            targets = values if program == "install" and "-d" in args else values[-1:]
            if target_dir is not None:
                targets = [target_dir]
            for target in targets:
                output_ok |= require_contained(target, program)
        if program == "truncate":
            for target in operands(args, {"-s", "--size", "-r", "--reference"}):
                output_ok |= require_contained(target, "truncate")
        if program == "mkdir":
            targets = operands(args, {"-m", "--mode"})
            for target in targets:
                require_contained(target, "mkdir")
            if "-p" in args and any(under_output_dir(target) for target in targets):
                mkdir_ok = True
        if program == "tar":
            traditional_flags = (
                args[0]
                if args
                and re.fullmatch(r"[A-Za-z]+", args[0])
                and any(flag in args[0] for flag in "xcruAt")
                else ""
            )
            short_flags = "".join(
                token[1:] for token in args if token.startswith("-") and not token.startswith("--")
            )
            short_flags += traditional_flags
            extracts = "x" in short_flags or "--extract" in args or "--get" in args
            writes_archive = any(flag in short_flags for flag in "cruA") or any(
                token in args for token in ("--create", "--append", "--update", "--catenate")
            )
            if "--to-command" in args or any(token.startswith("--to-command=") for token in args):
                fail(f"poc {finding_id}: tar --to-command is unsupported nested execution")
            directory = None
            archive = (
                args[1]
                if traditional_flags and "f" in traditional_flags and len(args) > 1
                else None
            )
            for index, token in enumerate(args):
                if token in {"-C", "--directory"} and index + 1 < len(args):
                    directory = args[index + 1]
                elif token.startswith("--directory="):
                    directory = token.split("=", 1)[1]
                if token in {"-f", "--file"} and index + 1 < len(args):
                    archive = args[index + 1]
                elif token.startswith("--file="):
                    archive = token.split("=", 1)[1]
                elif token.startswith("-") and not token.startswith("--") and "f" in token[1:]:
                    suffix = token[1:].split("f", 1)[1]
                    archive = suffix or (args[index + 1] if index + 1 < len(args) else None)
            if extracts:
                if directory is None:
                    fail(f"poc {finding_id}: tar extraction requires -C under POC_OUTPUT_DIR")
                require_contained(directory, "tar extraction")
            if writes_archive:
                if archive is None:
                    fail(f"poc {finding_id}: tar archive writer requires an explicit file")
                output_ok |= require_contained(archive, "tar archive")
        if program == "unzip":
            listing = any(token in {"-l", "-t", "-Z", "--list"} for token in args)
            if not listing:
                directory = None
                for index, token in enumerate(args):
                    if token in {"-d", "--directory"} and index + 1 < len(args):
                        directory = args[index + 1]
                    elif token.startswith("--directory="):
                        directory = token.split("=", 1)[1]
                if directory is None:
                    fail(f"poc {finding_id}: unzip extraction requires -d under POC_OUTPUT_DIR")
                require_contained(directory, "unzip extraction")
    if not mkdir_ok:
        fail(f"poc {finding_id}: script must create POC_OUTPUT_DIR with mkdir -p")
    if not output_ok:
        fail(f"poc {finding_id}: script must write an artifact under POC_OUTPUT_DIR")


def _validate_browser_recipe(recipe: object, poc: dict, finding_id: str) -> set[str]:
    required = {
        "_header", "finding_id", "config_keys", "steps",
        "success_criteria", "why_not_scriptable",
    }
    if not isinstance(recipe, dict) or set(recipe) != required:
        fail(
            f"poc {finding_id}: browser recipe must contain exactly _header, "
            "finding_id, config_keys, steps, success_criteria, why_not_scriptable"
        )
    _require_poc_header(recipe["_header"], finding_id)
    if recipe["finding_id"] != finding_id:
        fail(f"poc {finding_id}: browser recipe finding_id must match its manifest entry")
    config_keys = recipe["config_keys"]
    if not isinstance(config_keys, list) or config_keys != poc["placeholders"]:
        fail(f"poc {finding_id}: browser config_keys must exactly match placeholders")
    steps = recipe["steps"]
    if not isinstance(steps, list) or not steps:
        fail(f"poc {finding_id}: browser recipe requires non-empty steps")
    step_fields = {
        "navigate": {"action", "url"},
        "fill": {"action", "selector", "value"},
        "click": {"action", "selector"},
        "assert": {"action", "kind", "expect"},
    }
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("action") not in step_fields:
            fail(f"poc {finding_id}: browser step {index} has a non-canonical action")
        expected = step_fields[step["action"]]
        if set(step) != expected or any(
            not isinstance(step[key], str) or not step[key].strip()
            for key in expected - {"action"}
        ):
            fail(
                f"poc {finding_id}: browser step {index} must exactly match its "
                f"{step['action']} schema"
            )
    _require_nonempty_string(recipe, "success_criteria", f"poc {finding_id}")
    _require_nonempty_string(recipe, "why_not_scriptable", f"poc {finding_id}")
    step_text = json.dumps(steps)
    references = set(re.findall(r"\$([A-Z][A-Z0-9_]*)", step_text))
    if references != set(config_keys):
        fail(
            f"poc {finding_id}: browser step parameter references must exactly match "
            "config_keys"
        )
    return set(config_keys)


def _validate_script_contract(
    workspace: pathlib.Path,
    expected_dir: pathlib.Path,
    poc: dict,
    finding_id: str,
) -> None:
    script_path = _require_nonempty_string(poc, "script_path", f"poc {finding_id}")
    relative = pathlib.PurePosixPath(script_path)
    expected_prefix = pathlib.PurePosixPath("output", "proof_of_concept", expected_dir.name)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parent != expected_prefix
        or not relative.name.startswith(f"{finding_id}-")
    ):
        fail(
            f"poc {finding_id}: script_path must be a direct child of "
            f"output/proof_of_concept/{expected_dir.name}/ named {finding_id}-*"
        )
    script = (workspace / pathlib.Path(*relative.parts)).resolve()
    try:
        script.relative_to(expected_dir)
    except ValueError:
        fail(f"poc {finding_id}: script_path escapes its repository PoC directory")
    if not script.is_file() or script.stat().st_size == 0:
        fail(f"poc {finding_id}: script_path does not exist or is empty")
    content = script.read_text()
    script_type = poc["script_type"]
    suffixes = {
        "curl-shell": (".sh",),
        "source-check": (".sh",),
        "python": (".py",),
        "browser-recipe": (".browser.json",),
        "other": (".sh", ".py"),
    }[script_type]
    if not any(script.name.endswith(suffix) for suffix in suffixes):
        fail(f"poc {finding_id}: script extension does not match script_type")
    if re.search(r"<[A-Z][A-Z0-9_]*>", content):
        fail(f"poc {finding_id}: script contains a forbidden angle-bracket placeholder")
    uncommented = _strip_hash_comments(content)

    placeholders = poc["placeholders"]
    declared: set[str]
    if script_type == "browser-recipe":
        try:
            recipe = json.loads(content)
        except json.JSONDecodeError as exc:
            fail(f"poc {finding_id}: browser recipe is not valid JSON: {exc}")
        declared = _validate_browser_recipe(recipe, poc, finding_id)
        inspected = json.dumps(recipe["steps"])
        if (
            re.search(
                r"(?i)(?:[a-z0-9.-]+\.)?organization\.com\b|\bapi\.|\bproduction\b",
                inspected,
            )
            or re.search(r"https?://(?!\$\{|\$[A-Za-z_])\S+", inspected)
        ):
            fail(
                f"poc {finding_id}: browser steps contain a forbidden literal "
                "host or environment"
            )
    elif script.name.endswith(".sh"):
        _require_poc_header(
            "\n".join(
                line.lstrip()[1:]
                for line in content.splitlines()
                if line.lstrip().startswith("#")
                and not line.lstrip().startswith("#!")
            ),
            finding_id,
        )
        check = subprocess.run(
            ["bash", "-n", str(script)], text=True, capture_output=True, check=False
        )
        if check.returncode:
            fail(f"poc {finding_id}: bash syntax check failed: {check.stderr.strip()}")
        active = uncommented
        if (
            re.search(
                r"(?i)(?:[a-z0-9.-]+\.)?organization\.com\b|\bapi\.|\bprod(?:uction)?\b",
                active,
            )
            or re.search(r"https?://(?!\$\{|\$[A-Za-z_])\S+", active)
        ):
            fail(f"poc {finding_id}: script contains a forbidden literal host or environment")
        if re.search(
            r"(?m)^\s*(?:source|\.)\s+[^\n]*(?:poc-config\.env|/\.env|\.env\b)",
            active,
        ) or re.search(r"\bload_config\b", active):
            fail(f"poc {finding_id}: script must not source a shared config")
        sourced = re.findall(r"(?m)^\s*(?:source|\.)\s+([^\n]+)$", active)
        if sourced and (
            script_type != "source-check"
            or any("poc-common.sh" not in source for source in sourced)
        ):
            fail(
                f"poc {finding_id}: only source-check scripts may source "
                "lib/poc-common.sh"
            )
        if script_type == "source-check":
            helper = workspace / "output" / "proof_of_concept" / "lib" / "poc-common.sh"
            expected_helper = (
                workspace.resolve()
                / "output" / "proof_of_concept" / "lib" / "poc-common.sh"
            )
            try:
                resolved_helper = helper.resolve(strict=True)
            except (FileNotFoundError, OSError):
                resolved_helper = None
            if (
                helper.is_symlink()
                or resolved_helper != expected_helper
                or resolved_helper is None
                or not resolved_helper.is_file()
                or resolved_helper.stat().st_size == 0
            ):
                fail(f"poc {finding_id}: source-check requires lib/poc-common.sh")
            canonical_source = re.compile(
                r'''(?m)^\s*(?:source|\.)\s+(?:
                    ["']?\$_here/\.\./lib/poc-common\.sh["']?
                    |
                    "\$\(cd\s+"\$_here/\.\."\s+&&\s+pwd\)/lib/poc-common\.sh"
                )\s*$''',
                re.VERBOSE,
            )
            canonical_here = re.compile(
                r'''(?m)^\s*_here="\$\(cd\s+"\$\(dirname\s+"\$0"\)"\s+&&\s+pwd\)"\s*$'''
            )
            if (
                len(sourced) != 1
                or canonical_source.search(active) is None
                or canonical_here.search(active) is None
            ):
                fail(
                    f"poc {finding_id}: source-check must source the canonical "
                    "output/proof_of_concept/lib/poc-common.sh helper"
                )
            if re.search(
                r'''(?m)^\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*=\s*["']?\$\(\s*)?ensure_source(?:\s|["'])''',
                active,
            ) is None:
                fail(f"poc {finding_id}: source-check must call ensure_source")
        commands = _shell_commands(active, finding_id)
        if script_type == "curl-shell" and not any(
            command and command[0] == "curl" for command in commands
        ):
            fail(f"poc {finding_id}: curl-shell must execute curl")
        declarations = re.findall(
            r"(?m)^\s*([A-Z][A-Z0-9_]*)=\"\$\{\1:-([^}]*)\}\"([^\n]*)$",
            content,
        )
        internal = {"POC_OUTPUT_DIR"}
        if script_type == "source-check":
            internal.add("SOURCE_DIR_ROOT")
        declared = {key for key, _default, _tail in declarations} - internal
        for key, default, tail in declarations:
            if key in placeholders and not default and "# FILL:" not in tail:
                fail(f"poc {finding_id}: blank {key} requires a # FILL: comment")
        _validate_shell_output_wiring(active, finding_id, commands)
        references = {
            first or second
            for first, second in re.findall(
                r"\$(?:\{([A-Z][A-Z0-9_]*)[^}]*\}|([A-Z][A-Z0-9_]*))",
                active,
            )
        }
        assigned = set(
            re.findall(r"(?m)^\s*(?:local\s+)?([A-Z][A-Z0-9_]*)=", active)
        )
        missing_parameters = references - assigned - {"BASH_SOURCE"}
        if missing_parameters:
            fail(
                f"poc {finding_id}: script references undeclared parameters: "
                f"{sorted(missing_parameters)}"
            )
    else:
        _require_poc_header(
            "\n".join(
                line.lstrip()[1:]
                for line in content.splitlines()
                if line.lstrip().startswith("#")
                and not line.lstrip().startswith("#!")
            ),
            finding_id,
        )
        try:
            tree = ast.parse(content, str(script))
        except SyntaxError as exc:
            fail(f"poc {finding_id}: Python syntax check failed: {exc}")
        active = uncommented
        if (
            re.search(
                r"(?i)(?:[a-z0-9.-]+\.)?organization\.com\b|\bapi\.|\bprod(?:uction)?\b",
                active,
            )
            or re.search(r"https?://(?!\$\{|\$[A-Za-z_])\S+", active)
        ):
            fail(f"poc {finding_id}: script contains a forbidden literal host or environment")
        if re.search(r"(?:poc-config\.env|dotenv|load_config)", active, re.IGNORECASE):
            fail(f"poc {finding_id}: Python script must not read a shared config")
        declarations = re.findall(
            r'''(?m)^\s*([A-Z][A-Z0-9_]*)\s*=\s*os\.environ\.get\(\s*["']\1["']\s*,\s*["']([^"']*)["']\s*\)([^\n]*)$''',
            content,
        )
        declared = {key for key, _default, _tail in declarations} - {"POC_OUTPUT_DIR"}
        for key, default, tail in declarations:
            if key in placeholders and not default and "# FILL:" not in tail:
                fail(f"poc {finding_id}: blank {key} requires a # FILL: comment")
        output_assignment = any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "POC_OUTPUT_DIR"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in ast.walk(tree)
        )
        output_mkdir = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdir"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "POC_OUTPUT_DIR"
            for node in ast.walk(tree)
        )
        output_use = any(
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id == "POC_OUTPUT_DIR"
            for node in ast.walk(tree)
        )
        if not output_assignment or not output_mkdir or not output_use:
            fail(f"poc {finding_id}: Python script must wire artifacts under POC_OUTPUT_DIR")
        environment_keys = set(
            re.findall(
                r'''os\.environ(?:\.get\(\s*|\[\s*)["']([A-Z][A-Z0-9_]*)["']''',
                active,
            )
        )
        unexpected_keys = environment_keys - set(placeholders) - {"POC_OUTPUT_DIR"}
        if unexpected_keys:
            fail(
                f"poc {finding_id}: Python script reads undeclared parameters: "
                f"{sorted(unexpected_keys)}"
            )
    if declared != set(placeholders):
        fail(
            f"poc {finding_id}: inline parameter declarations must exactly match "
            f"placeholders; missing={sorted(set(placeholders) - declared)}, "
            f"extra={sorted(declared - set(placeholders))}"
        )


def validate_manifest(
    path: pathlib.Path,
    candidates_path: pathlib.Path,
    *,
    slug: str,
    date: str,
    dataflow_path: pathlib.Path | None = None,
) -> tuple[dict, dict[str, dict], dict[str, dict], dict[str, dict]]:
    candidates = validate_candidates(
        candidates_path,
        dataflow_path=dataflow_path,
        post_debate=True,
        slug=slug if dataflow_path is not None else None,
        date=date if dataflow_path is not None else None,
        require_validation=True,
    )
    manifest = load_object(path, "PoC manifest")
    workspace, expected_dir = _manifest_workspace(path, slug)
    if manifest.get("schema_version") != "4":
        fail('PoC manifest schema_version must be exactly "4"')
    if manifest.get("repo_slug") != slug:
        fail(f"PoC manifest repo_slug must match state slug {slug!r}")
    if manifest.get("date") != date:
        fail(f"PoC manifest date must match state date {date!r}")
    pocs = manifest.get("pocs")
    skipped = manifest.get("skipped")
    if not isinstance(pocs, list) or not isinstance(skipped, list):
        fail("PoC manifest pocs and skipped must be lists")
    if not candidates:
        if pocs:
            fail("no-candidates manifest must have empty pocs")
        if len(skipped) != 1 or not isinstance(skipped[0], dict):
            fail("no-candidates manifest must contain exactly one skipped marker")
        marker = skipped[0]
        if marker.get("reason") != "no-candidates" or "finding_id" in marker:
            fail("no-candidates manifest requires one manifest-level no-candidates marker")
        _require_nonempty_string(marker, "note", "no-candidates marker")
        if manifest.get("config_path") is not None or manifest.get("guide_path") is not None:
            fail("no-candidates manifest config_path and guide_path must be null")
        return manifest, candidates, {}, {}

    expected_config = f"output/proof_of_concept/{slug}/poc-config.env"
    expected_guide = f"output/proof_of_concept/{slug}/POC-GUIDE-{date}.md"
    if pocs:
        if manifest.get("config_path") != expected_config:
            fail(f"PoC manifest config_path must be {expected_config}")
        if manifest.get("guide_path") != expected_guide:
            fail(f"PoC manifest guide_path must be {expected_guide}")
        config = workspace / expected_config
        guide = workspace / expected_guide
        if not config.is_file() or config.stat().st_size == 0:
            fail(f"PoC reference catalog missing or empty: {expected_config}")
        if not guide.is_file() or guide.stat().st_size == 0:
            fail(f"PoC guide missing or empty: {expected_guide}")
        invoker = workspace / "output" / "proof_of_concept" / "run-all.sh"
        if not invoker.is_file() or invoker.stat().st_size == 0:
            fail(
                "PoC convenience runner missing or empty: "
                "output/proof_of_concept/run-all.sh"
            )
        config_text = config.read_text()
        guide_text = guide.read_text()
        if not re.search(r"reference[- ]only|reference only", config_text, re.IGNORECASE):
            fail("PoC reference catalog must identify itself as reference-only")
        if not re.search(r"not source|never source", config_text, re.IGNORECASE):
            fail("PoC reference catalog must state that scripts do not source it")
    elif manifest.get("config_path") is not None or manifest.get("guide_path") is not None:
        fail("manifest with empty pocs must set config_path and guide_path to null")

    poc_by_id: dict[str, dict] = {}
    skip_by_id: dict[str, dict] = {}
    for kind, entries, destination in (
        ("poc", pocs, poc_by_id), ("skipped", skipped, skip_by_id),
    ):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                fail(f"{kind} entry at index {index} must be an object")
            finding_id = entry.get("finding_id")
            if not isinstance(finding_id, str) or re.fullmatch(r"F-[0-9]{3}", finding_id) is None:
                fail(f"{kind} entry at index {index}: finding_id must match F-NNN")
            if finding_id in destination:
                fail(f"PoC manifest contains duplicate finding_id {finding_id} in {kind} entries")
            destination[finding_id] = entry
    overlap = set(poc_by_id) & set(skip_by_id)
    if overlap:
        fail(f"PoC manifest finding_id appears in both pocs and skipped: {sorted(overlap)}")
    covered = set(poc_by_id) | set(skip_by_id)
    expected = set(candidates)
    if covered != expected:
        fail(
            "PoC manifest finding_id coverage must exactly match candidates; "
            f"missing={sorted(expected - covered)}, extra={sorted(covered - expected)}"
        )

    for finding_id, candidate in candidates.items():
        entry = poc_by_id.get(finding_id) or skip_by_id[finding_id]
        if entry.get("tier") != candidate["final_tier"]:
            fail(f"PoC manifest {finding_id}: tier does not match candidate Final Tier")
        if entry.get("disposition") != candidate["disposition"]:
            fail(
                f"PoC manifest {finding_id}: disposition does not match candidate "
                "Final Disposition"
            )
        eligible = (
            candidate["final_tier"] in {"P0", "P1", "P2"}
            and candidate["disposition"] != "DISMISSED"
        )
        if finding_id in poc_by_id:
            if not eligible:
                fail(f"PoC manifest {finding_id}: ineligible finding cannot appear in pocs")
            poc = poc_by_id[finding_id]
            for key in (
                "title", "script_path", "script_type", "confirm_path",
                "generated_by", "verdict",
            ):
                _require_nonempty_string(poc, key, f"poc {finding_id}")
            if poc["script_type"] not in POC_TYPES:
                fail(f"poc {finding_id}: non-canonical script_type {poc['script_type']!r}")
            if poc["confirm_path"] not in CONFIRM_PATHS:
                fail(f"poc {finding_id}: non-canonical confirm_path {poc['confirm_path']!r}")
            if poc["verdict"] not in POC_VERDICTS:
                fail(f"poc {finding_id}: non-canonical verdict {poc['verdict']!r}")
            placeholders = poc.get("placeholders")
            if not isinstance(placeholders, list) or any(
                not isinstance(value, str)
                or PLACEHOLDER.fullmatch(value) is None
                for value in placeholders
            ):
                fail(
                    f"poc {finding_id}: placeholders must be an UPPER_SNAKE_CASE string list"
                )
            if len(placeholders) != len(set(placeholders)):
                fail(f"poc {finding_id}: placeholders must not contain duplicates")
            confirm_note = poc.get("confirm_note")
            if poc["confirm_path"] == "script":
                if confirm_note is not None:
                    fail(f"poc {finding_id}: script confirmation requires confirm_note null")
                if poc["script_type"] == "browser-recipe":
                    fail(f"poc {finding_id}: browser-recipe cannot use confirm_path script")
            else:
                if not isinstance(confirm_note, str) or not confirm_note.strip():
                    fail(
                        f"poc {finding_id}: {poc['confirm_path']} confirmation requires "
                        "a non-empty confirm_note"
                    )
                if poc["confirm_path"] == "browser" and poc["script_type"] != "browser-recipe":
                    fail(f"poc {finding_id}: confirm_path browser requires browser-recipe")
            _validate_reviewer_envelopes(poc, finding_id)
            _validate_script_contract(workspace, expected_dir, poc, finding_id)
            guide_text = (workspace / expected_guide).read_text()
            config_text = (workspace / expected_config).read_text()
            if finding_id not in guide_text:
                fail(f"poc {finding_id}: PoC guide must contain a finding section")
            for placeholder in placeholders:
                if placeholder not in guide_text:
                    fail(f"poc {finding_id}: PoC guide omits {placeholder}")
                catalog_definition = re.findall(
                    rf"(?m)^\s*{re.escape(placeholder)}=", config_text
                )
                if len(catalog_definition) != 1:
                    fail(
                        f"poc {finding_id}: reference catalog must define {placeholder} "
                        "exactly once"
                    )
                block_start = config_text.rfind("\n", 0, config_text.find(placeholder))
                nearby = config_text[max(0, block_start - 500):config_text.find(placeholder)]
                for label in ("What:", "Used by:", "Why:"):
                    if label not in nearby:
                        fail(
                            f"poc {finding_id}: reference catalog {placeholder} "
                            f"block is missing {label}"
                        )
        else:
            skip = skip_by_id[finding_id]
            required_skip = {"finding_id", "tier", "disposition", "reason", "note"}
            if not required_skip.issubset(skip):
                fail(
                    f"skipped {finding_id}: missing required tier/disposition/reason/note"
                )
            reason = skip.get("reason")
            if reason not in SKIP_REASONS:
                fail(f"skipped {finding_id}: non-canonical reason {reason!r}")
            _require_nonempty_string(skip, "note", f"skipped {finding_id}")
            if eligible and reason not in FAILURE_SKIP_REASONS:
                fail(
                    f"skipped {finding_id}: eligible P0-P2 finding requires an explicit "
                    "generation/review failure reason"
                )
            if candidate["final_tier"] in {"P3", "P4"} and reason != "not-eligible-tier":
                fail(f"skipped {finding_id}: P3/P4 finding requires not-eligible-tier")
            if (
                candidate["final_tier"] in {"P0", "P1", "P2"}
                and candidate["disposition"] == "DISMISSED"
                and reason != "not-eligible-disposition"
            ):
                fail(f"skipped {finding_id}: DISMISSED finding requires not-eligible-disposition")
    return manifest, candidates, poc_by_id, skip_by_id


def _validate_repo_metadata(
    findings: dict,
    receipt: dict,
    *,
    mode: str,
    slug: str,
    date: str,
    expected_multitenant_scope: bool | None,
) -> None:
    if findings.get("schema_version") != "2":
        fail('schema_version must be exactly "2"')
    if receipt.get("schema_version") != "1":
        fail('receipt schema_version must be exactly "1"')
    if findings.get("repo_slug") != slug or receipt.get("repo_slug") != slug:
        fail(f"findings and receipt repo_slug must match state slug {slug!r}")
    if findings.get("date") != date or receipt.get("review_date") != date:
        fail(f"findings and receipt dates must match state date {date!r}")
    for key in ("repo_url", "commit_sha", "multitenant_scope"):
        if key not in findings or key not in receipt:
            fail(f"findings and receipt must both contain {key}")
        if findings[key] != receipt[key]:
            fail(f"{key} must exactly match receipt-{date}.json")
    repo_url = findings["repo_url"]
    commit_sha = findings["commit_sha"]
    multitenant_scope = findings["multitenant_scope"]
    valid_repo_url = isinstance(repo_url, str) and (
        repo_url.startswith("https://")
        or repo_url.startswith("ssh://")
        or re.fullmatch(r"git@[^:]+:.+", repo_url) is not None
    )
    if mode == "review":
        if not valid_repo_url:
            fail("repo_url must be a supported HTTPS or SSH clone URL in review mode")
        if not isinstance(commit_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha) is None:
            fail("commit_sha must be a full 40-character hexadecimal SHA in review mode")
        if not isinstance(multitenant_scope, bool):
            fail("multitenant_scope must be a JSON boolean in review mode")
        if expected_multitenant_scope is None or multitenant_scope != expected_multitenant_scope:
            fail("multitenant_scope does not match phase-0 run log")
    elif mode == "triage":
        if repo_url != "n/a (triage)" and not valid_repo_url:
            fail("repo_url must be a supported clone URL or 'n/a (triage)' in triage mode")
        if commit_sha != "n/a (triage)" or multitenant_scope != "n/a (triage)":
            fail("triage commit_sha and multitenant_scope must be 'n/a (triage)'")
    else:
        fail(f"unsupported review mode: {mode!r}")


def _nonempty_string(finding: dict, key: str, fid: str) -> str:
    return _require_nonempty_string(finding, key, f"finding {fid}")


def _context_catalog_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label} must be a non-empty repository-relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.startswith(
        "references/organization_context/"
    ):
        fail(f"{label} must be under references/organization_context/")


def _validate_report_metadata(finding: dict, fid: str) -> None:
    controls = finding.get("controls_in_scope", [])
    if not isinstance(controls, list):
        fail(f"finding {fid}: controls_in_scope must be a list")
    seen_controls: set[tuple[str, object]] = set()
    for index, control in enumerate(controls):
        label = f"finding {fid}: controls_in_scope[{index}]"
        if not isinstance(control, dict) or set(control) != CONTROL_FIELDS:
            fail(f"{label} must contain exactly name, control_id, note_path")
        name = _require_nonempty_string(control, "name", label)
        control_id = control["control_id"]
        if control_id is not None and (
            not isinstance(control_id, str)
            or re.fullmatch(r"CTRL-[1-9][0-9]*", control_id) is None
        ):
            fail(f"{label}: control_id must be null or CTRL-NNNN")
        _context_catalog_path(control["note_path"], f"{label}.note_path")
        key = (name, control_id)
        if key in seen_controls:
            fail(f"finding {fid}: controls_in_scope contains duplicates")
        seen_controls.add(key)

    policies = finding.get("applicable_policies", [])
    if not isinstance(policies, list):
        fail(f"finding {fid}: applicable_policies must be a list")
    seen_policies: set[str] = set()
    for index, policy in enumerate(policies):
        label = f"finding {fid}: applicable_policies[{index}]"
        if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
            fail(
                f"{label} must contain exactly id, title, binding_statement, note_path"
            )
        policy_id = _require_nonempty_string(policy, "id", label)
        if re.fullmatch(r"STD-[0-9]{4}", policy_id) is None:
            fail(f"{label}: id must be STD-NNNN")
        _require_nonempty_string(policy, "title", label)
        _require_nonempty_string(policy, "binding_statement", label)
        _context_catalog_path(policy["note_path"], f"{label}.note_path")
        if policy_id in seen_policies:
            fail(f"finding {fid}: applicable_policies contains duplicate ids")
        seen_policies.add(policy_id)


def _validate_receipt(receipt: dict, findings_by_id: dict[str, dict]) -> None:
    entries = receipt.get("findings")
    if not isinstance(entries, list):
        fail("receipt findings must be a list")
    by_id: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            fail(f"receipt finding at index {index} must be an object")
        finding_id = entry.get("id")
        if not isinstance(finding_id, str) or re.fullmatch(r"F-[0-9]{3}", finding_id) is None:
            fail(f"receipt finding at index {index}: id must match F-NNN")
        if finding_id in by_id:
            fail(f"receipt contains duplicate finding id {finding_id}")
        by_id[finding_id] = entry
    if set(by_id) != set(findings_by_id):
        fail(
            "receipt finding IDs must exactly match final findings; "
            f"missing={sorted(set(findings_by_id) - set(by_id))}, "
            f"extra={sorted(set(by_id) - set(findings_by_id))}"
        )
    for finding_id, finding in findings_by_id.items():
        entry = by_id[finding_id]
        if entry.get("tier_final") != finding["tier"]:
            fail(f"receipt {finding_id}: tier_final does not match findings JSON")
        scoring = entry.get("scoring_final")
        expected = {
            "cvss_score": finding["cvss_score"],
            "cvss_vector": finding["cvss_vector"],
            "tier": finding["tier"],
        }
        if scoring != expected:
            fail(f"receipt {finding_id}: scoring_final does not match findings JSON")
    analyzed_files = receipt.get("analyzed_files")
    if not isinstance(analyzed_files, list):
        fail("receipt analyzed_files must be a list")
    valid_ids = set(findings_by_id)
    for index, analyzed in enumerate(analyzed_files):
        if not isinstance(analyzed, dict):
            fail(f"receipt analyzed_files[{index}] must be an object")
        referenced_by = analyzed.get("referenced_by")
        if not isinstance(referenced_by, list) or any(
            not isinstance(value, str) for value in referenced_by
        ):
            fail(f"receipt analyzed_files[{index}].referenced_by must be a string list")
        if len(referenced_by) != len(set(referenced_by)):
            fail(f"receipt analyzed_files[{index}].referenced_by contains duplicates")
        unknown = set(referenced_by) - valid_ids
        if unknown:
            fail(
                f"receipt analyzed_files[{index}].referenced_by contains unknown "
                f"finding IDs: {sorted(unknown)}"
            )


def validate_findings(
    path: pathlib.Path,
    receipt_path: pathlib.Path,
    candidates_path: pathlib.Path,
    manifest_path: pathlib.Path,
    *,
    mode: str,
    slug: str,
    date: str,
    expected_multitenant_scope: bool | None,
    dataflow_path: pathlib.Path | None,
) -> None:
    document = load_object(path, "findings JSON")
    receipt = load_object(receipt_path, "receipt JSON")
    manifest, candidates, poc_by_id, skip_by_id = validate_manifest(
        manifest_path,
        candidates_path,
        slug=slug,
        date=date,
        dataflow_path=dataflow_path,
    )
    _validate_repo_metadata(
        document,
        receipt,
        mode=mode,
        slug=slug,
        date=date,
        expected_multitenant_scope=expected_multitenant_scope,
    )
    unknown_top = set(document) - ALLOWED_REPORT_TOP_LEVEL
    if unknown_top:
        fail(f"findings JSON contains unknown top-level fields: {sorted(unknown_top)}")
    for key in REQUIRED_REPORT_TOP_LEVEL:
        if key not in document:
            fail(f"findings JSON missing field: {key}")
    dismissed = document["dismissed_findings"]
    if not isinstance(dismissed, list):
        fail("dismissed_findings must be a list")
    for index, item in enumerate(dismissed):
        if not isinstance(item, dict) or set(item) != {"finding_id", "title", "reason"}:
            fail(f"dismissed_findings[{index}] must contain exactly finding_id, title, reason")
        for key in ("finding_id", "title", "reason"):
            _nonempty_string(item, key, f"dismissed_findings[{index}]")
    expected_dismissed = [
        {
            "finding_id": finding_id,
            "title": candidate["dismissed_title"],
            "reason": candidate["dismissal_reason"],
        }
        for finding_id, candidate in sorted(candidates.items())
        if candidate["disposition"] == "DISMISSED"
    ]
    if dismissed != expected_dismissed:
        fail(
            "dismissed_findings must exactly match sorted post-debate DISMISSED "
            "candidate IDs, titles, and dismissal reasons"
        )
    methodology = document["methodology_notes"]
    if not isinstance(methodology, list) or not methodology or any(
        not isinstance(note, str) or not note.strip() for note in methodology
    ):
        fail("methodology_notes must be a non-empty string list")
    if not isinstance(document["disclaimer"], str) or not document["disclaimer"].strip():
        fail("disclaimer must be a non-empty string")
    findings = document.get("findings")
    if not isinstance(findings, list):
        fail("findings must be a list")
    expected_ids = {
        finding_id
        for finding_id, candidate in candidates.items()
        if candidate["disposition"] != "DISMISSED"
    }
    findings_by_id: dict[str, dict] = {}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            fail(f"finding at index {index} must be an object")
        fid = str(finding.get("finding_id") or f"<index {index}>")
        unknown_finding = set(finding) - ALLOWED_FINDING_FIELDS
        if unknown_finding:
            fail(f"finding {fid}: unknown fields: {sorted(unknown_finding)}")
        for key in REQUIRED_FINDING_TEXT:
            _nonempty_string(finding, key, fid)
        for key in ("cwe", "cve", "poc"):
            if key not in finding:
                fail(f"finding {fid}: missing field: {key}")
        references = finding.get("references")
        if not isinstance(references, list) or not references or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in references
        ):
            fail(f"finding {fid}: references must be a non-empty string list")
        _validate_report_metadata(finding, fid)
        if re.fullmatch(r"F-[0-9]{3}", fid) is None:
            fail(f"finding {fid}: finding_id must match F-NNN")
        if fid in findings_by_id:
            fail(f"finding {fid}: duplicate finding_id")
        findings_by_id[fid] = finding
        score_value = finding.get("cvss_score")
        if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
            fail(f"finding {fid}: cvss_score must be numeric")
        claimed = float(score_value)
        vector = finding["cvss_vector"].strip()
        try:
            computed = score_base_vector(vector)
        except CVSS4Error as exc:
            fail(f"finding {fid}: invalid CVSS vector: {exc}")
        if claimed != computed:
            fail(
                f"finding {fid}: CVSS score {claimed:.1f} does not match "
                f"vector score {computed:.1f}"
            )
        if finding["tier"].strip() != tier_for_score(computed):
            fail(f"finding {fid}: tier does not match CVSS score")
        if finding["exposure"] not in EXPOSURES:
            fail(f"finding {fid}: exposure must use the canonical enum")
        if finding["validation"] not in VALIDATIONS:
            fail(f"finding {fid}: validation must use the canonical enum")
        cwe = finding["cwe"]
        if cwe is not None and (
            not isinstance(cwe, str) or re.fullmatch(r"CWE-[1-9][0-9]*", cwe) is None
        ):
            fail(f"finding {fid}: cwe must be null or CWE-NNN")
        cve = finding["cve"]
        if cve is not None and (
            not isinstance(cve, str)
            or re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", cve) is None
        ):
            fail(f"finding {fid}: cve must be null or CVE-YYYY-NNNN")
    if set(findings_by_id) != expected_ids:
        fail(
            "final finding IDs must exactly match non-dismissed candidates; "
            f"missing={sorted(expected_ids - set(findings_by_id))}, "
            f"extra={sorted(set(findings_by_id) - expected_ids)}"
        )
    for fid, finding in findings_by_id.items():
        candidate = candidates[fid]
        if finding["cvss_score"] != candidate["final_score"]:
            fail(f"finding {fid}: cvss_score does not match candidate Final CVSS-B Score")
        if finding["cvss_vector"] != candidate["final_vector"]:
            fail(f"finding {fid}: cvss_vector does not match candidate Final CVSS Vector")
        if finding["tier"] != candidate["final_tier"]:
            fail(f"finding {fid}: tier does not match candidate Final Tier")
        if finding["exposure"] != candidate["exposure"]:
            fail(f"finding {fid}: exposure does not match candidate exposure")
        if finding["validation"] != candidate.get("validation"):
            fail(
                f"finding {fid}: validation does not match its record-level "
                "candidate Validation result"
            )
        if fid in poc_by_id:
            poc = poc_by_id[fid]
            expected_poc = {
                "script_path": poc["script_path"],
                "script_type": poc["script_type"],
                "confirm_path": poc["confirm_path"],
                "verdict": poc["verdict"],
                "guide_path": manifest.get("guide_path"),
                "config_path": manifest.get("config_path"),
                "placeholders": poc["placeholders"],
            }
        else:
            skipped = skip_by_id[fid]
            expected_poc = {
                "skipped": True,
                "reason": skipped["reason"],
                "note": skipped["note"],
            }
        if finding["poc"] != expected_poc:
            fail(f"finding {fid}: poc tagged union does not exactly match PoC manifest")
    expected_coverage = {
        "generated": sum(1 for finding in findings if "script_path" in finding["poc"]),
        "skipped": sum(1 for finding in findings if finding["poc"].get("skipped") is True),
    }
    if document["poc_coverage"] != expected_coverage:
        fail("poc_coverage does not match finding PoC tagged unions")
    _validate_receipt(receipt, findings_by_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dataflow = sub.add_parser("dataflow")
    dataflow.add_argument("--input", type=pathlib.Path, required=True)
    dataflow.add_argument("--slug", required=True)
    dataflow.add_argument("--date", required=True)
    candidates = sub.add_parser("candidates")
    candidates.add_argument("--input", type=pathlib.Path, required=True)
    candidates.add_argument("--dataflow", type=pathlib.Path)
    candidates.add_argument("--post-debate", action="store_true")
    candidates.add_argument("--require-validation", action="store_true")
    candidates.add_argument("--slug")
    candidates.add_argument("--date")
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--input", type=pathlib.Path, required=True)
    manifest.add_argument("--candidates", type=pathlib.Path, required=True)
    manifest.add_argument("--slug", required=True)
    manifest.add_argument("--date", required=True)
    manifest.add_argument("--dataflow", type=pathlib.Path)
    findings = sub.add_parser("findings")
    findings.add_argument("--input", type=pathlib.Path, required=True)
    findings.add_argument("--receipt", type=pathlib.Path, required=True)
    findings.add_argument("--candidates", type=pathlib.Path, required=True)
    findings.add_argument("--manifest", type=pathlib.Path, required=True)
    findings.add_argument("--mode", choices=("review", "triage"), required=True)
    findings.add_argument("--slug", required=True)
    findings.add_argument("--date", required=True)
    findings.add_argument("--expected-multitenant-scope", choices=("true", "false"))
    findings.add_argument("--dataflow", type=pathlib.Path)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "dataflow":
            validate_dataflow(args.input, slug=args.slug, date=args.date)
        elif args.command == "candidates":
            validate_candidates(
                args.input,
                dataflow_path=args.dataflow,
                post_debate=args.post_debate,
                slug=args.slug,
                date=args.date,
                require_validation=args.require_validation,
            )
        elif args.command == "manifest":
            validate_manifest(
                args.input,
                args.candidates,
                slug=args.slug,
                date=args.date,
                dataflow_path=args.dataflow,
            )
        else:
            expected_scope = (
                None
                if args.expected_multitenant_scope is None
                else args.expected_multitenant_scope == "true"
            )
            validate_findings(
                args.input,
                args.receipt,
                args.candidates,
                args.manifest,
                mode=args.mode,
                slug=args.slug,
                date=args.date,
                expected_multitenant_scope=expected_scope,
                dataflow_path=args.dataflow,
            )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
