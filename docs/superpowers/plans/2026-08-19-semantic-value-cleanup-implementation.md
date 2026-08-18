# Semantic Value Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `subagent-driven-development` or `executing-plans` to implement this plan
> task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a Skill-driven semantic candidate workflow that validates
source-linked capsule bundles, renders safe text-only replacements, preserves
sidecar provenance, and applies only after independent audit and exact
confirmation.

**Architecture:** The Skill produces a JSON semantic bundle containing the
intent map, work-block decisions, capsule text, and source evidence. The
standard-library Python helper validates that bundle, materializes a candidate
JSONL, audits it, and applies it through the existing backup/replace path. The
canonical JSONL gains no new record type; only an allowlisted old tool-output
`payload.output` can become an existing `input_text` node. Legacy profiles
remain unchanged.

**Tech Stack:** Python 3 standard library, JSONL, pytest, Markdown.

**Spec:** `docs/superpowers/specs/2026-08-19-semantic-value-cleanup-design.md`

## Global Constraints

- The Skill owns semantic value decisions; the executor recomputes source
  identity, target paths, hashes, and structural invariants.
- Unknown, protected, visible, structural, code, JSON, patch, configuration,
  unique-evidence, and ambiguous-image content is never auto-compressed.
- Semantic candidates use `plan_version: 4` and `audit_version: 3`; legacy
  version 3 plans and version 2 audits remain supported for legacy profiles but
  are rejected for semantic candidates.
- Semantic audit stages are ordered: `schema`, `semantic_review`, `policy`,
  `deterministic_transform`, `integrity`.
- The apply step never calls a model and requires exact `plan_id` confirmation,
  a verified original backup, a passing current audit, and an unchanged source.
- Source/raw-line hashes use original UTF-8 JSONL bytes including newline;
  sidecar hashes use sorted-key canonical UTF-8 JSON without a trailing
  newline; rendered text uses UTF-8 with normalized `\n`.
- Only an old tool-output record whose `payload.output` is a list of nodes
  containing exactly `type: "input_text"` and string `text` fields is eligible
  for the first semantic in-place compatibility profile.
- Existing tests and legacy commands must remain green.

---

### Task 1: Define And Validate Semantic Bundles

**Files:**
- Create: `references/semantic-bundle-schema.md`
- Modify: `scripts/session_cleanup.py:28-60, 1266-1281`
- Test: `tests/test_session_cleanup.py`

**Interfaces:**
- Add `SEMANTIC_PLAN_VERSION = 4` and
  `SEMANTIC_AUDIT_VERSION = 3` without changing legacy constants.
- Add `canonical_json_bytes(value: Any) -> bytes` for sidecar hashing.
- Add `hash_json_node(value: Any) -> str` using the declared node hash domain.
- Add `is_semantic_text_output(value: Any) -> bool` for the exact first
  compatibility profile.
- Add `validate_semantic_bundle(bundle: Any, source_records: list[dict[str,
  Any]], raw_lines: list[bytes], protected_from: int) -> list[str]`.
- Add `validate_semantic_operation(operation: Any, record: dict[str, Any],
  raw_line: bytes, protected_from: int) -> list[str]`.

- [x] **Step 1: Write failing validation tests**

Add tests with these names and behaviors:

Add `import copy` beside the existing test imports. The helper below is a
fixture function used by the tests in Tasks 1-3.

```python
def _semantic_fixture(temp_dir):
    # Write one old custom_tool_call_output with a text-only output and return
    # source, records, raw_lines, protected_from, and a valid bundle that
    # targets its /payload/output node.
    source = temp_dir / "session.jsonl"
    record = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "call-1",
            "output": [{"type": "input_text", "text": "old output"}],
        },
    }
    raw = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    source.write_bytes(raw)
    records, raw_lines, errors = parse_jsonl(source)
    assert errors == []
    rendered_text = "[session cleanup capsule]\nretained: old output"
    operation = {
        "block_id": "b-1",
        "line": 1,
        "record_index": 0,
        "call_id": "call-1",
        "json_pointer": "/payload/output",
        "source_node_sha256": hash_json_node(record["payload"]["output"]),
        "rendered_text": rendered_text,
    }
    bundle = {
        "semantic_map_version": 1,
        "source": {"sha256": sha256_file(source), "bytes": len(raw)},
        "blocks": [{"block_id": "b-1", "source_lines": [1, 1], "role": "context"}],
        "operations": [operation],
        "sidecar": {"capsule_id": "capsule-1", "rendered_text": rendered_text},
    }
    bundle_path = temp_dir / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return {
        "source": source,
        "records": records,
        "raw_lines": raw_lines,
        "protected_from": 99,
        "bundle": bundle,
        "bundle_path": bundle_path,
    }

def test_semantic_bundle_accepts_source_bound_text_operation(self):
    # A valid old tool-output record outside the protected region has a
    # payload.output list of input_text nodes and an operation targeting
    # /payload/output with the matching source node hash.
    fixture = _semantic_fixture(self.temp_dir)
    assert validate_semantic_bundle(
        fixture["bundle"], fixture["records"], fixture["raw_lines"],
        fixture["protected_from"],
    ) == []

def test_semantic_bundle_rejects_visible_and_protected_operations(self):
    # A user/assistant record or a line at protected_from produces a blocking
    # error even when the model bundle marks it low value.
    fixture = _semantic_fixture(self.temp_dir)
    errors = validate_semantic_bundle(
        fixture["bundle"], fixture["records"], fixture["raw_lines"], 1,
    )
    assert "protected_or_visible_record" in errors

def test_semantic_bundle_rejects_structured_output_and_wrong_pointer(self):
    # Mixed input_text/input_image output, dict output, or any pointer other
    # than /payload/output is not eligible for the first profile.
    fixture = _semantic_fixture(self.temp_dir)
    structured_records = copy.deepcopy(fixture["records"])
    structured_records[0]["payload"]["output"].append(
        {"type": "input_image", "image_url": "https://example.invalid/image.png"}
    )
    errors = validate_semantic_bundle(
        fixture["bundle"], structured_records, fixture["raw_lines"],
        fixture["protected_from"],
    )
    assert errors

def test_semantic_bundle_rejects_stale_call_id_or_node_hash(self):
    # The bundle cannot authorize an operation against a changed record.
    fixture = _semantic_fixture(self.temp_dir)
    stale_bundle = copy.deepcopy(fixture["bundle"])
    stale_bundle["operations"][0]["call_id"] = "call-stale"
    errors = validate_semantic_bundle(
        stale_bundle, fixture["records"], fixture["raw_lines"],
        fixture["protected_from"],
    )
    assert "source_identity_mismatch" in errors
```

- [x] **Step 2: Run the focused tests and verify they fail for missing helpers**

Run:

```powershell
pytest tests/test_session_cleanup.py -k semantic_bundle -v
```

Expected: collection succeeds and the new tests fail because the semantic
validation interfaces do not exist.

- [x] **Step 3: Document the bundle contract**

Write the exact JSON fields for `semantic_map`, `blocks`, `operations`,
`sidecar`, source identity, and review metadata in
`references/semantic-bundle-schema.md`. State that model prose is advisory and
executor-owned identity fields are recomputed.

- [x] **Step 4: Implement hash and validation primitives**

Implement the interfaces above with standard-library JSON and SHA-256 only.
Reject missing source lines, duplicate block IDs, duplicate target lines,
unknown JSON pointers, missing `call_id`, protected/visible records, mixed
output nodes, and mismatched raw-line/node hashes. Do not mutate records in
these helpers.

- [x] **Step 5: Run focused and legacy tests**

Run:

```powershell
pytest tests/test_session_cleanup.py -k "semantic_bundle or hash or output" -v
pytest -q
```

Expected: new tests and the existing baseline pass.

- [x] **Step 6: Commit the validated contract**

```powershell
git add references/semantic-bundle-schema.md scripts/session_cleanup.py tests/test_session_cleanup.py
git commit -m "feat: validate semantic cleanup bundles"
```

### Task 2: Materialize Semantic Candidates

**Files:**
- Modify: `scripts/session_cleanup.py:722-811, 1035-1165, 2652-2735`
- Modify: `references/jsonl-schema.md`
- Test: `tests/test_session_cleanup.py`

**Interfaces:**
- Add `render_capsule_node(rendered_text: str) -> list[dict[str, str]]`.
- Add `materialize_semantic_candidate(source: Path, candidate: Path,
  bundle: dict[str, Any], records: list[dict[str, Any]],
  raw_lines: list[bytes], protected_from: int) -> dict[str, Any]`.
- Add `build_semantic_plan(source: Path, report_dir: Path, bundle_path: Path,
  recent_records: int, recent_compactions: int) -> dict[str, Any]`.
- Add a `semantic-plan` CLI command with positional `target`, required
  `--bundle`, optional `--codex-home`, `--report-dir`, `--recent-records`,
  and `--recent-compactions`.

- [ ] **Step 1: Write failing materialization tests**

Add tests with these names:

Use `_semantic_fixture(self.temp_dir)` from Task 1. Each test unpacks
`source`, `bundle_path`, and a temporary `candidate` path from that fixture.

```python
def test_materialize_semantic_candidate_replaces_only_declared_output(self):
    # The candidate keeps every raw line except the declared old tool-output
    # line and renders the approved capsule as one input_text node.
    fixture = _semantic_fixture(self.temp_dir)
    candidate = self.temp_dir / "candidate.jsonl"
    result = materialize_semantic_candidate(
        fixture["source"], candidate, fixture["bundle"], fixture["records"],
        fixture["raw_lines"], fixture["protected_from"],
    )
    candidate_records, _, _ = parse_jsonl(candidate)
    assert result["changed_records"] == 1
    assert candidate_records[0]["payload"]["call_id"] == fixture["records"][0]["payload"]["call_id"]

def test_semantic_plan_contains_sidecar_and_v4_metadata(self):
    # The plan records candidate_kind, semantic map digest, sidecar digest,
    # operation hashes, compatibility profile, and plan_version 4.
    fixture = _semantic_fixture(self.temp_dir)
    plan = build_semantic_plan(
        fixture["source"], self.temp_dir, fixture["bundle_path"], 1000, 2,
    )
    assert plan["plan_version"] == 4
    assert plan["candidate_kind"] == "semantic_cleanup"

def test_semantic_plan_blocks_when_no_safe_operation_exists(self):
    # A bundle containing only unknown/structured blocks produces a blocked or
    # preview-only plan and never writes a changed candidate.
    fixture = _semantic_fixture(self.temp_dir)
    unsafe_bundle = copy.deepcopy(fixture["bundle"])
    unsafe_bundle["operations"] = []
    unsafe_bundle_path = self.temp_dir / "unsafe-bundle.json"
    unsafe_bundle_path.write_text(json.dumps(unsafe_bundle), encoding="utf-8")
    plan = build_semantic_plan(
        fixture["source"], self.temp_dir, unsafe_bundle_path, 1000, 2,
    )
    assert plan["status"] in {"blocked", "no_change"}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

```powershell
pytest tests/test_session_cleanup.py -k "materialize or semantic_plan" -v
```

Expected: failures for missing materialization and CLI integration.

- [ ] **Step 3: Implement deterministic capsule rendering**

Render only the already-approved text into a list containing one
`{"type":"input_text","text": ...}` node. Preserve all outer record fields,
line endings for unchanged lines, and the `payload` fields outside
`/payload/output`. Copy the sidecar bundle into the report directory with a
content digest; do not add a new JSONL record.

- [ ] **Step 4: Implement semantic plan creation**

Load and validate the bundle, inspect the source, calculate the protected
region, materialize the candidate, and write a plan containing `plan_version:
4`, source/candidate fingerprints, `candidate_kind: semantic_cleanup`,
`semantic_map`, sidecar fingerprint, operations, compatibility evidence,
review requirements, and block-level retained/omitted explanations.

- [ ] **Step 5: Add and test the `semantic-plan` CLI route**

Wire parser and `main()` without changing legacy `plan` behavior. Return exit
code 0 only for a reviewable semantic candidate or an explicit no-change
result; return 2 for blocked validation. Test the command through `main()` with
temporary JSONL and bundle files.

- [ ] **Step 6: Update the schema reference and run tests**

Document that semantic candidates alter only eligible `payload.output` nodes,
while sidecar data stays outside canonical JSONL. Run:

```powershell
pytest tests/test_session_cleanup.py -k "semantic or plan" -v
pytest -q
```

- [ ] **Step 7: Commit the candidate materializer**

```powershell
git add scripts/session_cleanup.py references/jsonl-schema.md tests/test_session_cleanup.py
git commit -m "feat: materialize semantic cleanup candidates"
```

### Task 3: Audit And Apply Semantic Candidates

**Files:**
- Modify: `scripts/session_cleanup.py:1366-1991, 2171-2582`
- Test: `tests/test_session_cleanup.py`

**Interfaces:**
- Add semantic audit stages while preserving the legacy stage tuple for
  `plan_version: 3` candidates.
- Add `audit_semantic_review_stage(source: Path, candidate: Path,
  plan: dict[str, Any]) -> dict[str, Any]`.
- Add `audit_semantic_transform_stage(source: Path, candidate: Path,
  plan: dict[str, Any]) -> dict[str, Any]`.
- Add sidecar verification to `audit_matches_current_files()` and
  `apply_plan()`.
- Add manifest states `backup_verified`, `sidecar_staged`,
  `candidate_staged`, `source_replaced`, `verified`, `success`, `failed`,
  and `needs_manual_recovery` for semantic apply batches.

- [ ] **Step 1: Write failing audit tests**

Add tests with these names:

Use the semantic fixture and unpack `plan_path`, `plan_id`, `backup_root`,
`sidecar_path`, and `batch_dir` from the test setup. Existing tests use
`unittest.TestCase`, so use `self.assertRaises` rather than pytest assertions.

```python
def test_semantic_audit_requires_ordered_v3_stages(self):
    audit = audit_plan(Path(plan_path))
    assert audit["audit_version"] == 3
    assert [stage["name"] for stage in audit["stages"]] == [
        "schema", "semantic_review", "policy", "deterministic_transform", "integrity"
    ]

def test_semantic_audit_rejects_changed_sidecar_or_rendered_text(self):
    # Editing either artifact after plan creation invalidates the audit.
    sidecar_path.write_bytes(sidecar_path.read_bytes() + b"x")
    audit = audit_plan(Path(plan_path))
    assert audit["status"] == "fail"

def test_semantic_apply_binds_backup_id_without_changing_reviewed_capsule(self):
    # Backup is verified first; manifest receives backup_id while the reviewed
    # sidecar/candidate hashes remain unchanged.
    result = apply_plan(Path(plan_path), plan_id, backup_root)
    assert result["status"] == "success"

def test_semantic_apply_rejects_stale_source_or_missing_sidecar(self):
    with self.assertRaises(ValueError):
        apply_plan(Path(plan_path), plan_id, backup_root)

def test_semantic_reconciliation_never_guesses_after_replace_crash(self):
    # A prepared batch is either verified, restored, or reported as manual
    # recovery; it is never silently marked successful.
    state = reconcile_apply_batch(batch_dir)
    assert state["status"] in {"success", "failed", "needs_manual_recovery"}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

```powershell
pytest tests/test_session_cleanup.py -k "semantic_audit or semantic_apply or reconciliation" -v
```

Expected: failures because semantic stages and lifecycle validation are not
implemented.

- [ ] **Step 3: Implement semantic audit stages**

Use the bundle's stored hashes as inputs, recompute all source/candidate/
sidecar identities, validate the reviewed semantic bundle, replay the renderer,
and compare exact candidate bytes. Reject legacy audit versions for semantic
plans and reject semantic audit versions for legacy plans.

- [ ] **Step 4: Implement sidecar-aware apply ordering**

Preserve the existing byte-for-byte backup requirement. For semantic plans,
verify source and audit first, create the backup and `backup_id`, stage and
hash the sidecar, stage the candidate, atomically replace the source, verify
the final hash, then mark the manifest successful. Never regenerate capsule
text or mutate the reviewed sidecar during apply.

- [ ] **Step 5: Implement idempotent reconciliation and failure reporting**

On an interrupted batch, compare source, candidate, sidecar, and backup hashes.
Mark a fully verified replacement successful, restore only from the verified
original backup when the candidate is not active, and otherwise return
`needs_manual_recovery` with all paths and hashes. Preserve orphaned and
failed artifacts.

- [ ] **Step 6: Run audit/apply and full tests**

```powershell
pytest tests/test_session_cleanup.py -k "semantic_audit or semantic_apply or reconciliation" -v
pytest -q
python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py
```

- [ ] **Step 7: Commit the audited apply path**

```powershell
git add scripts/session_cleanup.py tests/test_session_cleanup.py
git commit -m "feat: audit and apply semantic cleanup plans"
```

### Task 4: Make The Skill Adaptive And Document The Workflow

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `references/semantic-bundle-schema.md`
- Test: `tests/test_session_cleanup.py` for CLI/documented contract smoke cases

- [ ] **Step 1: Write documentation contract checks**

Add a small test or validation script assertion that `SKILL.md` names the
semantic workflow, the exact `semantic-plan` command, the four user-facing
steps, hard-stop behavior, and exact `plan_id` confirmation. The check must
fail until the new instructions are present.

- [ ] **Step 2: Update Skill instructions**

Teach the Skill to inspect first, summarize the conversation in natural
language, create the semantic bundle, run the independent critic, present at
most three user-level choices, and call `semantic-plan` only with a validated
bundle. Keep hashes and audit fields in the safety-details section. Explain
that the helper changes disk history, not an already-loaded context.

- [ ] **Step 3: Update README and schema references**

Document the bundle authoring contract, sidecar location, candidate comparison,
semantic audit stages, apply ordering, restore behavior, and legacy profile
compatibility. Include one complete synthetic command flow.

- [ ] **Step 4: Run the documented smoke flow and full verification**

```powershell
pytest -q
python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py
git diff --check
```

- [ ] **Step 5: Commit Skill and documentation updates**

```powershell
git add SKILL.md README.md references/semantic-bundle-schema.md tests/test_session_cleanup.py
git commit -m "docs: expose adaptive semantic cleanup workflow"
```

## Final Verification

After all tasks, run the complete test suite, a synthetic
`semantic-plan -> audit -> apply -> restore` flow, and the existing legacy
`inspect -> plan -> audit -> apply` flow. Confirm that source bytes remain
unchanged before apply, that the original backup is byte-for-byte identical,
that semantic sidecar and candidate digests match the manifest, and that no
legacy tests regress.

Before merge, run the repository's code-review workflow against the complete
branch diff. Resolve all Critical and Important findings before integration;
record any deliberately deferred Minor findings in the implementation review.
