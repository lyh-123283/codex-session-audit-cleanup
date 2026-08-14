# Adaptive Cleanup Candidates and Backup Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the Codex session cleanup skill to derive an explicit cleanup intent, generate independently auditable candidates with different strengths, support deterministic target-size plans, and provide a separately confirmed backups cleanup workflow.

**Architecture:** Keep the existing standard-library implementation in scripts/session_cleanup.py and add a small immutable policy/profile layer instead of splitting the mature single-file helper. A plan-set JSON document indexes independent PLAN_VERSION=3 candidate plans; each candidate retains its own source hash, candidate file, audit file, transformation policy, and confirmation ID. Existing single-plan audit/apply and backups prune remain valid through explicit compatibility paths, while new CLI output exposes plan-set comparison and backup preview metadata.

**Tech Stack:** Python 3.10+, standard library only, unittest, PowerShell CLI smoke tests, Markdown skill documentation.

## Global Constraints

- Process one unambiguous regular .jsonl session file per invocation and never modify session_index.jsonl, SQLite/WAL/SHM files, locks, active context, or other sessions.
- Default retention is the two most recent logical compaction boundaries; every profile preserves visible user/assistant messages, user-message images, unknown fields, record count, tool calls, call IDs, and JSONL record structure byte-for-byte.
- Only old custom_tool_call_output and function_call_output records may change; no complete record deletion, age-only deletion, model summary, or silent fallback to a stronger profile is allowed.
- Named profiles are the only source of their parameters. User thresholds are valid only through explicit custom/target mode and must be recorded in plan metadata.
- Every candidate is independently audited and bound to the source SHA-256, candidate SHA-256, plan digest, audit digest, profile metadata, and selected boundary metadata.
- apply requires the exact candidate plan_id; a plan-set ID, stale ID, vague approval, or a plan missing policy metadata is rejected.
- Every apply creates and hash-verifies a byte-for-byte original backup; failed and unknown backup batches are never automatically deleted.
- Backup cleanup is explicit and separate from apply; it keeps at least one valid successful recovery point, defaults to two, treats invalid timestamps as preserved, and uses preview snapshot verification plus quarantine rollback.
- Use only Python's standard library and preserve the existing unittest test style.

## File Map

- Modify scripts/session_cleanup.py: profile resolution, intent metadata, plan-set generation, target-size selection, independent bundle audit, backup cleanup alias/filters/preview schema, and CLI parser wiring.
- Modify tests/test_session_cleanup.py: regression tests for profile policies, candidate sets, target sizing, old-plan rejection, backup age filtering, preview binding, aliases, and CLI smoke behavior.
- Modify SKILL.md: adaptive intent workflow, three candidate strengths, exact candidate confirmation, target infeasibility, and backups cleanup instructions.
- Modify README.md: public CLI examples, JSON output fields, compatibility notes, and verification commands.
- Modify references/jsonl-schema.md: policy metadata and audit invariants required by plan version 3.
- Create docs/superpowers/plans/2026-08-14-adaptive-cleanup-candidates-and-backup-management.md: this implementation record.

## Task 1: Add Explicit Cleanup Profiles and Two-Boundary Defaults

**Files:**
- Modify scripts/session_cleanup.py lines 23-32 and 363-528
- Test tests/test_session_cleanup.py lines 1-104 and 107-258

**Interfaces:**
- PROFILE_POLICIES: dict[str, dict[str, Any]] exposes cache, balanced, and space with immutable policy values.
- resolve_profile_policy(profile, max_output_bytes, prefix_bytes, suffix_bytes) returns recorded policy metadata and rejects mixed named-profile/threshold input.
- transform_lines(lines, boundary, recent_records, policy) keeps the existing eligible-record rules and applies the resolved policy.
- The existing build_plan threshold arguments remain callable by old tests; when they are explicitly supplied the plan records profile custom instead of silently labeling the plan balanced.

- [ ] Step 1: Write failing profile tests. Add these tests to SessionCleanupTests.

    def test_profiles_have_expected_thresholds(self):
        self.assertEqual(session_cleanup.DEFAULT_RECENT_COMPACTIONS, 2)
        self.assertIsNone(session_cleanup.PROFILE_POLICIES["cache"]["max_output_bytes"])
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["max_output_bytes"], 64 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["prefix_bytes"], 8 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["suffix_bytes"], 4 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["max_output_bytes"], 16 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["prefix_bytes"], 2 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["suffix_bytes"], 1 * 1024)

    def test_named_profile_rejects_manual_thresholds(self):
        with self.assertRaises(ValueError):
            session_cleanup.resolve_profile_policy("balanced", 4096, None, None)

    def test_cache_profile_clears_old_tool_images_without_text_truncation(self):
        lines, boundary = self._make_old_tool_output_with_image_and_large_text()
        result, summary = session_cleanup.transform_lines(
            lines,
            boundary,
            1000,
            session_cleanup.resolve_profile_policy("cache", None, None, None),
        )
        self.assertEqual(summary["image_payloads_cleared"], 1)
        self.assertEqual(summary["truncated_outputs"], 0)
        self.assertIn("[older tool output image cache cleared]", result[0])
        self.assertIn("x" * 65536, result[0])

- [ ] Step 2: Run the focused tests and verify they fail for the missing profile API.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_profiles_have_expected_thresholds tests.test_session_cleanup.SessionCleanupTests.test_named_profile_rejects_manual_thresholds tests.test_session_cleanup.SessionCleanupTests.test_cache_profile_clears_old_tool_images_without_text_truncation -v

Expected: FAIL because the default compaction count and profile resolver do not yet expose the new contract.

- [ ] Step 3: Implement the profile table and policy resolver. Replace the old defaults while retaining threshold validation for custom policies.

    DEFAULT_RECENT_COMPACTIONS = 2
    PLAN_VERSION = 3
    PROFILE_POLICIES = {
        "cache": {
            "profile": "cache",
            "scrub_images": True,
            "max_output_bytes": None,
            "prefix_bytes": None,
            "suffix_bytes": None,
        },
        "balanced": {
            "profile": "balanced",
            "scrub_images": True,
            "max_output_bytes": 64 * 1024,
            "prefix_bytes": 8 * 1024,
            "suffix_bytes": 4 * 1024,
        },
        "space": {
            "profile": "space",
            "scrub_images": True,
            "max_output_bytes": 16 * 1024,
            "prefix_bytes": 2 * 1024,
            "suffix_bytes": 1 * 1024,
        },
    }

    def resolve_profile_policy(profile, max_output_bytes=None, prefix_bytes=None, suffix_bytes=None):
        if profile in PROFILE_POLICIES:
            if any(value is not None for value in (max_output_bytes, prefix_bytes, suffix_bytes)):
                raise ValueError("manual output thresholds require profile=custom")
            return dict(PROFILE_POLICIES[profile])
        if profile != "custom":
            raise ValueError("invalid profile: expected cache, balanced, space, or custom")
        validate_cleanup_options(1000, 1, max_output_bytes, prefix_bytes, suffix_bytes)
        return {
            "profile": "custom",
            "scrub_images": True,
            "max_output_bytes": max_output_bytes,
            "prefix_bytes": prefix_bytes,
            "suffix_bytes": suffix_bytes,
        }

Update transform_lines to call scrub_image_nodes only when policy["scrub_images"] is true, skip truncate_output when max_output_bytes is None, and return the existing summary keys plus a policy copy. Keep the old-output, old-boundary, and visible-message predicates unchanged.

- [ ] Step 4: Run the focused tests and the existing boundary suite.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_profiles_have_expected_thresholds tests.test_session_cleanup.SessionCleanupTests.test_named_profile_rejects_manual_thresholds tests.test_session_cleanup.SessionCleanupTests.test_cache_profile_clears_old_tool_images_without_text_truncation -v
    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_recent_compaction_boundary tests.test_session_cleanup.SessionCleanupTests.test_non_positive_options_are_rejected -v

Expected: all selected tests pass; tests that assert the old default of one compaction are updated in Task 2 to assert the new default of two.

- [ ] Step 5: Commit the isolated profile change.

    git add scripts/session_cleanup.py tests/test_session_cleanup.py
    git commit -m "feat: add adaptive cleanup profiles"

## Task 2: Generate and Audit Independent Candidate Plans

**Files:**
- Modify scripts/session_cleanup.py lines 363-687, 768-1084, 1131-1251, and 1615-1691
- Test tests/test_session_cleanup.py lines 260-376 and 705-760

**Interfaces:**
- build_intent_profile(stats, problem, retention_priority, allowed_strength, target_bytes) records problem, retention priority, allowed strength, assumptions, target, and evidence.
- build_plan(source_path, report_dir, recent_records, recent_compactions, profile="balanced", intent_profile=None, target_bytes=None, max_output_bytes=None, prefix_bytes=None, suffix_bytes=None) writes one candidate plan with plan_version 3, policy, and intent_profile.
- build_plan_set(target, report_dir, profiles, intent_profile, recent_records, recent_compactions) writes plan-set-SET_ID.json plus one plan/candidate per profile and excludes unchanged candidates.
- audit_plan continues to audit one candidate; audit_plan_set(plan_set_path) audits every indexed candidate independently and returns a comparison.
- apply_plan accepts only a candidate plan with plan_version 3, valid policy metadata, and an exact candidate plan_id; a plan-set path is rejected.

- [ ] Step 1: Write failing tests for intent metadata, candidate independence, and old-plan rejection.

    def test_plan_set_has_independent_profiles_and_intent(self):
        with tempfile.TemporaryDirectory() as root:
            source = self._make_profile_session(Path(root))
            result = session_cleanup.build_plan_set(
                source,
                Path(root) / "reports",
                ["cache", "balanced", "space"],
                {
                    "problem": "overall_size",
                    "retention_priority": "recent_content",
                    "allowed_strength": "balanced",
                    "target_bytes": None,
                },
            )
            self.assertEqual(result["status"], "ready_for_review")
            self.assertGreaterEqual(len(result["candidates"]), 1)
            plan_ids = {entry["plan_id"] for entry in result["candidates"]}
            self.assertEqual(len(plan_ids), len(result["candidates"]))
            self.assertEqual(
                {entry["source_sha256"] for entry in result["candidates"]},
                {result["source"]["sha256"]},
            )
            self.assertEqual(
                {entry["policy"]["profile"] for entry in result["candidates"]},
                {"cache", "balanced", "space"},
            )

    def test_audit_plan_set_audits_each_candidate(self):
        plan_set = self._build_audited_plan_set()
        audit = session_cleanup.audit_plan_set(plan_set["plan_set_path"])
        self.assertEqual(audit["status"], "pass")
        self.assertEqual(len(audit["candidate_audits"]), len(plan_set["candidates"]))
        self.assertTrue(all(item["status"] == "pass" for item in audit["candidate_audits"]))

    def test_apply_rejects_plan_set_and_old_plan(self):
        with self.assertRaises(ValueError):
            session_cleanup.apply_plan("reports/plan-set-00000000000000000000000000000000.json", "00000000000000000000000000000000")
        old_plan = {"plan_version": 2, "plan_id": "00000000000000000000000000000000"}
        old_path = Path(self.tempdir.name) / "old-plan.json"
        old_path.write_text(json.dumps(old_plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            session_cleanup.apply_plan(old_path, old_plan["plan_id"])

- [ ] Step 2: Run the new tests to establish the missing bundle contract.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_plan_set_has_independent_profiles_and_intent tests.test_session_cleanup.SessionCleanupTests.test_audit_plan_set_audits_each_candidate tests.test_session_cleanup.SessionCleanupTests.test_apply_rejects_plan_set_and_old_plan -v

Expected: FAIL because plan generation currently writes one plan and has no plan-set or policy metadata.

- [ ] Step 3: Add deterministic intent and candidate serialization. Implement build_intent_profile with explicit defaults: problem from inspect image/tool-output metrics (image_cache when image payloads exist, otherwise oversized_output when output bytes exceed 64 KiB, otherwise overall_size), retention_priority recent_content, allowed_strength balanced, and an assumptions list explaining each inferred value. Store all evidence as numeric inspect metrics, never as a guessed target. Include policy, intent_profile, boundary, and candidate_kind session_cleanup in every plan before calculating plan_digest.

- [ ] Step 4: Add build_plan_set and write independent files. Generate each requested profile from the same source snapshot, call build_plan with a fresh plan_id, and write the bundle with this schema:

    {
      "plan_set_version": 1,
      "plan_set_id": "32-hex-id",
      "status": "ready_for_review",
      "source": {"path": "absolute-session.jsonl", "sha256": "source-digest"},
      "intent_profile": {
        "problem": "overall_size",
        "retention_priority": "recent_content",
        "allowed_strength": "balanced",
        "target_bytes": null,
        "assumptions": [],
        "evidence": {}
      },
      "candidates": [
        {
          "plan_id": "candidate-plan-id",
          "plan_path": "plan-candidate-plan-id.json",
          "policy": {"profile": "balanced"},
          "candidate_bytes": 1234,
          "bytes_saved": 567,
          "audit_path": "audit-candidate-plan-id.json",
          "audit_status": "pending"
        }
      ]
    }

Use absolute paths for every stored path, bind the bundle to the source SHA-256, and omit a profile when its changed_records is zero. If every requested profile is unchanged, return status no_change without creating an applyable candidate.

- [ ] Step 5: Add independent plan-set audit and strict apply guards. audit_plan_set rereads the source and each candidate, invokes the existing four stages through audit_plan, compares every candidate source hash to the bundle source hash, and writes each audit before writing the bundle candidate_audits. apply_plan rejects plan_set_version, plan_version != PLAN_VERSION, absent policy, absent boundary, absent intent_profile, and any profile/threshold mismatch. Keep the old four stage names and make deterministic audit compare the complete policy dictionary.

- [ ] Step 6: Wire the CLI without changing confirmation meaning. Make plan with no --profile generate the plan set for cache, balanced, and space; make plan --profile cache|balanced|space|custom|target generate one candidate. audit accepts either a candidate plan or a plan-set path. apply accepts only a candidate plan. Add --problem, --retention-priority, --allowed-strength, and --target-bytes; reject --target-bytes unless --profile target is explicit, and reject threshold flags unless --profile custom is explicit. Print the complete comparison and each candidate's exact plan_id.

- [ ] Step 7: Run the candidate workflow tests and commit.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_plan_set_has_independent_profiles_and_intent tests.test_session_cleanup.SessionCleanupTests.test_audit_plan_set_audits_each_candidate tests.test_session_cleanup.SessionCleanupTests.test_apply_rejects_plan_set_and_old_plan -v
    python -m unittest discover -s tests -v
    git add scripts/session_cleanup.py tests/test_session_cleanup.py
    git commit -m "feat: generate independently auditable cleanup candidates"

Expected: all tests pass, including existing single-plan audit/apply tests updated to use plan_version 3 policy metadata.

## Task 3: Add Deterministic Target-Size Planning

**Files:**
- Modify scripts/session_cleanup.py lines 446-528, 598-687, 920-976, and 1615-1691
- Test tests/test_session_cleanup.py lines 760-850

**Interfaces:**
- select_target_policy(lines, boundary, recent_records, target_bytes) returns either a deterministic custom policy or an infeasible result with the protected-size floor.
- Target plans store target_bytes, selection_method, lower_profile, upper_profile, selected_max_output_bytes, remaining_protected_bytes, and infeasible status in policy/target metadata.
- The target policy is passed unchanged to transform_lines during plan creation, audit, and apply.

- [ ] Step 1: Write failing target-size tests.

    def test_target_between_profiles_uses_deterministic_custom_threshold(self):
        plan = self._make_target_plan(target_bytes=80 * 1024)
        self.assertEqual(plan["policy"]["profile"], "custom")
        self.assertEqual(plan["target"]["target_bytes"], 80 * 1024)
        self.assertEqual(plan["target"]["selection_method"], "binary_search_between_balanced_and_space")
        first = (
            plan["policy"]["max_output_bytes"],
            plan["policy"]["prefix_bytes"],
            plan["policy"]["suffix_bytes"],
        )
        second = self._make_target_plan(target_bytes=80 * 1024)
        self.assertEqual(
            first,
            (
                second["policy"]["max_output_bytes"],
                second["policy"]["prefix_bytes"],
                second["policy"]["suffix_bytes"],
            ),
        )

    def test_target_below_protected_floor_is_infeasible(self):
        plan = self._make_target_plan(target_bytes=1)
        self.assertEqual(plan["status"], "infeasible")
        self.assertGreater(plan["target"]["remaining_protected_bytes"], 0)

    def test_target_mode_requires_explicit_target_profile(self):
        with self.assertRaises(ValueError):
            session_cleanup.resolve_plan_profile("balanced", target_bytes=1000)

- [ ] Step 2: Run the target tests and verify they fail.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_target_between_profiles_uses_deterministic_custom_threshold tests.test_session_cleanup.SessionCleanupTests.test_target_below_protected_floor_is_infeasible tests.test_session_cleanup.SessionCleanupTests.test_target_mode_requires_explicit_target_profile -v

Expected: FAIL because target metadata and policy selection do not exist.

- [ ] Step 3: Implement deterministic selection. Add resolve_plan_profile(profile, target_bytes=None, max_output_bytes=None, prefix_bytes=None, suffix_bytes=None) as the single entry point for target/custom validation. Produce balanced and space candidate sizes from the same source and boundary. If the source already satisfies the target, return status no_change. If the target is below the space result, return status infeasible and set remaining_protected_bytes to the size of the space candidate plus all unchanged protected bytes. Otherwise binary-search the integer output threshold between 16 KiB and 64 KiB, using fixed preview interpolation:

    def interpolate_preview_bytes(max_output_bytes):
        prefix_bytes = min(8 * 1024, max(1, max_output_bytes // 2))
        suffix_bytes = min(4 * 1024, max(1, max_output_bytes - prefix_bytes - 1))
        return prefix_bytes, suffix_bytes

At each search step call the same pure transform used by audit, retain the smallest threshold whose candidate size is at most target_bytes, and record selection_method binary_search_between_balanced_and_space. Do not modify protected records to satisfy a target.

- [ ] Step 4: Bind target metadata to audit and apply. Include the complete target object in the plan digest, compare it during deterministic audit, and make apply_plan reject a target plan whose selected policy, target bytes, or source size differs. Infeasible plans print a reason and never create a candidate that apply can accept.

- [ ] Step 5: Run target and full tests, then commit.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_target_between_profiles_uses_deterministic_custom_threshold tests.test_session_cleanup.SessionCleanupTests.test_target_below_protected_floor_is_infeasible tests.test_session_cleanup.SessionCleanupTests.test_target_mode_requires_explicit_target_profile -v
    python -m unittest discover -s tests -v
    git add scripts/session_cleanup.py tests/test_session_cleanup.py
    git commit -m "feat: add deterministic target-size cleanup plans"

## Task 4: Extend Backup Listing and Add backups cleanup

**Files:**
- Modify scripts/session_cleanup.py lines 1253-1473 and 1615-1691
- Test tests/test_session_cleanup.py lines 379-558 and 850-1020

**Interfaces:**
- list_backups(backup_root, session_id=None, keep=2, now=None) retains existing fields and adds size_bytes, age_days, deletion_eligible, and deletion_reason.
- prune_snapshot(backup_root, session_id, keep=2, older_than_days=None, now=None) includes a complete ordered snapshot, detailed candidates, preservation reasons, retained recovery count, and reclaimable bytes.
- prune_backups(backup_root, session_id, keep=2, confirm=None, older_than_days=None, now=None) remains the shared implementation for both prune and cleanup.

- [ ] Step 1: Write failing backup cleanup tests.

    def test_backups_cleanup_age_filter_keeps_recent_and_invalid_timestamp(self):
        root, session_id = self._make_backup_batches()
        preview = session_cleanup.prune_backups(
            root,
            session_id,
            keep=2,
            older_than_days=30,
            now=datetime.datetime(2026, 8, 14, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["retained_valid_successful"], 2)
        self.assertIn("reclaimable_bytes", preview)
        self.assertTrue(all(item["age_days"] >= 30 for item in preview["candidates"]))
        self.assertIn("invalid timestamp", " ".join(preview["preserved_reasons"]))

    def test_backups_cleanup_alias_has_same_preview_snapshot_as_prune(self):
        root, session_id = self._make_backup_batches()
        prune = session_cleanup.prune_backups(root, session_id, keep=2)
        cleanup = session_cleanup.prune_backups(root, session_id, keep=2)
        self.assertEqual(prune["snapshot"], cleanup["snapshot"])

    def test_cleanup_preview_rejects_backup_set_change(self):
        root, session_id = self._make_backup_batches()
        preview = session_cleanup.prune_backups(root, session_id, keep=1)
        self._add_valid_backup(root, session_id)
        with self.assertRaises(ValueError):
            session_cleanup.prune_backups(
                root,
                session_id,
                keep=1,
                confirm=preview["preview_id"],
            )

    def test_cleanup_move_failure_restores_quarantine(self):
        root, session_id = self._make_backup_batches()
        preview = session_cleanup.prune_backups(root, session_id, keep=1)
        with mock.patch.object(session_cleanup.shutil, "move", side_effect=OSError("move failed")):
            with self.assertRaises(RuntimeError):
                session_cleanup.prune_backups(
                    root,
                    session_id,
                    keep=1,
                    confirm=preview["preview_id"],
                )
        self.assertTrue(Path(preview["candidates"][0]["path"]).exists())

- [ ] Step 2: Run the new backup tests and verify missing fields/alias behavior.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_backups_cleanup_age_filter_keeps_recent_and_invalid_timestamp tests.test_session_cleanup.SessionCleanupTests.test_backups_cleanup_alias_has_same_preview_snapshot_as_prune tests.test_session_cleanup.SessionCleanupTests.test_cleanup_preview_rejects_backup_set_change tests.test_session_cleanup.SessionCleanupTests.test_cleanup_move_failure_restores_quarantine -v

Expected: FAIL because age filtering, detailed preview fields, and the cleanup parser action are absent.

- [ ] Step 3: Add strict timestamp and eligibility helpers. Parse only ISO-8601 created_at values with an explicit timezone; use an injected now in tests and UTC in production. Mark missing/invalid timestamps preserved. Keep the newest max(1, keep) valid successful backups before applying the optional age filter. Add each backup's exact byte size and reason to list and preview data.

- [ ] Step 4: Extend the snapshot and confirmation digest. Preserve the existing path-containment and manifest integrity checks, but add created_at, status, integrity, size_bytes, backup_id, and original_sha256 to every ordered snapshot entry. Add older_than_days, kept_valid_successful, candidates, preserved_reasons, and reclaimable_bytes to the preview before hashing it. Confirmation must compare the entire snapshot plus filter options and preview digest; any add/remove/edit/reorder rejects the ID.

- [ ] Step 5: Keep quarantine rollback explicit. Move only valid eligible directories to .prune-quarantine/PREVIEW_ID in deterministic order. On any move failure, move already-quarantined directories back to their original paths and retain the preview plus all uncertain data. On deletion failure, leave quarantine and report its exact path. Do not delete failed, unknown, corrupt, integrity-invalid, or invalid-timestamp backups.

- [ ] Step 6: Add the cleanup CLI alias and compatibility tests. Register backups cleanup with the same --backup-root, --session-id, --keep, --older-than-days, and --confirm arguments as backups prune; dispatch both actions to the same prune_backups function. Reject keep < 1 and negative age values. Preserve the old prune JSON keys delete_count, delete_paths, preserved_paths, and invalid_reasons alongside the new fields.

- [ ] Step 7: Run backup tests and commit.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_backups_cleanup_age_filter_keeps_recent_and_invalid_timestamp tests.test_session_cleanup.SessionCleanupTests.test_backups_cleanup_alias_has_same_preview_snapshot_as_prune -v
    python -m unittest discover -s tests -v
    git add scripts/session_cleanup.py tests/test_session_cleanup.py
    git commit -m "feat: add confirmed backup cleanup workflow"

## Task 5: Update Skill, Schema, README, and CLI Smoke Coverage

**Files:**
- Modify SKILL.md
- Modify README.md
- Modify references/jsonl-schema.md
- Modify tests/test_session_cleanup.py

**Interfaces:**
- Documentation uses the same names as code: plan_set_version, plan_id, profile, target, audit_plan_set, backups cleanup, preview_id, and backup_id.
- The skill instructions require inspect -> intent profile -> candidates -> independent audits -> exact plan selection -> apply and explicitly state that only disk history changes.

- [ ] Step 1: Add a CLI smoke test that runs the full candidate workflow. Use subprocess.run with sys.executable, a temporary synthetic JSONL, and a temporary report directory. Assert that plan prints a plan-set ID and at most three candidate IDs, audit prints pass for each selected plan, and apply rejects a plan-set path before any source write. Add a second smoke test for backups cleanup preview then exact confirmation.

- [ ] Step 2: Run the smoke tests before documentation edits.

    python -m unittest tests.test_session_cleanup.SessionCleanupTests.test_cli_candidate_workflow tests.test_session_cleanup.SessionCleanupTests.test_cli_backup_cleanup_workflow -v

Expected: PASS after Task 4 CLI wiring; failures identify mismatched output keys before docs are updated.

- [ ] Step 3: Rewrite SKILL.md workflow sections. Document the inferred intent fields and assumptions first, then the three profiles: cache clears only old tool-output image caches, balanced uses 64 KiB with 8 KiB prefix/4 KiB suffix, and space uses 16 KiB with 2 KiB prefix/1 KiB suffix. State that the newest two logical compaction boundaries are protected by default, unchanged candidates are omitted, target mode is explicit, infeasible targets stop, every candidate is independently audited, and only one exact candidate plan_id may be confirmed. Add PowerShell examples using actual plan-set and candidate plan paths.

- [ ] Step 4: Update README examples and compatibility notes. Show --profile, --target-bytes, the comparison fields, the separate apply command, backups cleanup --older-than-days, invalid timestamp preservation, quarantine failure reporting, and backups prune as a shared compatibility alias. Replace old one-boundary/default-threshold language and explain that old plan_version: 2 files must be regenerated.

- [ ] Step 5: Extend references/jsonl-schema.md. Add weak-schema rules for policy, intent_profile, target, boundary, plan-set indexing, candidate IDs, and preview snapshot fields. State that audit must compare the full policy and target metadata, while unknown session records and user images remain protected.

- [ ] Step 6: Run the complete verification set and commit documentation.

    python -m unittest discover -s tests -v
    python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py
    git diff --check
    git add SKILL.md README.md references/jsonl-schema.md tests/test_session_cleanup.py
    git commit -m "docs: document adaptive candidates and backup cleanup"

## Task 6: Final Review, Installation, and Release Verification

**Files:**
- Modify only the implementation and documentation files above if verification exposes a defect.
- Verify the design spec and this plan.

- [ ] Step 1: Review the implementation against every design requirement. Confirm that no old default of one compaction remains, no new path mutates a non-tool-output record, no apply path accepts a plan-set, no cleanup path deletes a non-valid-success backup, and no automatic cleanup call exists after apply.

    rg -n "DEFAULT_RECENT_COMPACTIONS|PLAN_VERSION|backups cleanup|plan_set|target_bytes|deletion_eligible|\\.prune-quarantine" scripts/session_cleanup.py SKILL.md README.md references/jsonl-schema.md
    python -m unittest discover -s tests -v
    python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py

- [ ] Step 2: Run read-only CLI validation. Use a temporary synthetic session and report directory, run inspect, plan, audit, and backups list; verify inspect reports safe_to_plan, plan output contains the source SHA-256 and protected boundary, every candidate audit has four passing stages, and no source bytes change before apply.

- [ ] Step 3: Run repository validation and inspect the diff.

    python -m unittest discover -s tests -v
    git diff --check
    git status --short

Review the diff for accidental real session files, report files, backup directories, credentials, machine-specific paths, or generated cache output.

- [ ] Step 4: Commit any final correction and push the repository.

    git add scripts/session_cleanup.py tests/test_session_cleanup.py SKILL.md README.md references/jsonl-schema.md docs/superpowers/plans/2026-08-14-adaptive-cleanup-candidates-and-backup-management.md
    git commit -m "feat: complete adaptive session cleanup upgrade"
    git push origin main

Record the resulting commit hash and confirm that the installed local copy is the pushed version before reinstalling.

- [ ] Step 5: Reinstall the local skill with the repository cachebuster flow. Run the documented skill installer against the pushed main branch, verify C:\\Users\\lenovo\\.codex\\skills\\codex-session-audit-cleanup\\SKILL.md contains the new profile and cleanup instructions, and run the installed script's import smoke command without touching any real session. Report the installed commit/version and the fact that existing active conversation context was not modified.

## Self-Review Checklist

- Spec coverage: Tasks 1-3 cover profiles, two boundaries, intent metadata, independent candidates, deterministic target mode, protected content, audits, and old-plan rejection; Task 4 covers cleanup previews, keep/age rules, invalid backup preservation, quarantine rollback, and aliases; Task 5 covers CLI workflow and all user-facing documentation; Task 6 covers verification and installation.
- Completion scan: the plan contains no deferred implementation marker; commands use concrete repository paths and test names.
- Type consistency: PROFILE_POLICIES, resolve_profile_policy, build_intent_profile, build_plan_set, audit_plan_set, select_target_policy, list_backups, prune_snapshot, and prune_backups are named consistently.
- Compatibility: old direct single-plan callers remain supported only through explicit candidate plans; old plan files are rejected by version and metadata checks; backups prune dispatches to the same code as backups cleanup.
- Safety: no step permits source replacement before independent audit and exact confirmation; every destructive backup operation rechecks the complete snapshot and uses quarantine.
