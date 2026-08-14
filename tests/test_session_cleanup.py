import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import session_cleanup  # noqa: E402
from session_cleanup import (  # noqa: E402
    apply_plan,
    audit_plan,
    build_plan,
    list_backups,
    logical_compaction_boundaries,
    prune_backups,
    restore_backup,
    sha256_file,
    self_digest,
)


def write_manifest(path, manifest):
    manifest["manifest_digest"] = self_digest(manifest, "manifest_digest")
    path.write_text(json.dumps(manifest), encoding="utf-8")


def write_record(handle, record):
    handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def make_session(path):
    old_image_output = [
        {"type": "input_text", "text": "old tool text"},
        {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 512},
    ]
    old_large_output = [{"type": "input_text", "text": "x" * 70000}]
    recent_large_output = [{"type": "input_text", "text": "recent" + "y" * 70000}]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Keep this"}],
                },
            },
        )
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "call-image"},
            },
        )
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-image",
                    "output": old_image_output,
                },
            },
        )
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "call-large"},
            },
        )
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-large",
                    "output": old_large_output,
                },
            },
        )
        write_record(handle, {"type": "compacted", "payload": {"summary": "recent boundary"}})
        write_record(
            handle,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-recent",
                    "output": recent_large_output,
                },
            },
        )


class SessionCleanupTests(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "session.jsonl"
            make_session(source)
            records, raw_lines, errors = session_cleanup.parse_jsonl(source)
            self.assertEqual(errors, [])

            transformed = session_cleanup.transform_lines(
                records,
                raw_lines,
                protected_from=7,
                policy=session_cleanup.resolve_profile_policy("cache"),
            )
            candidate = b"".join(transformed["candidate_lines"])

            self.assertEqual(transformed["image_payloads_cleared"], 1)
            self.assertEqual(transformed["truncated_outputs"], 0)
            self.assertIn(b"[image cache cleared]", candidate)
            self.assertIn(b"x" * 70000, candidate)

    def test_audit_rejects_missing_policy_metadata_in_plan_v3(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan = build_plan(source, root / "reports")
            plan.pop("policy")
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("policy metadata" in error for error in audit["errors"]))

    def test_audit_rejects_policy_mismatch_between_plan_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan = build_plan(source, root / "reports")
            plan["transformation"]["policy"] = session_cleanup.PROFILE_POLICIES["space"]
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("policy metadata" in error for error in audit["errors"]))

    def test_recent_compactions_preserves_from_second_latest_logical_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"

            with source.open("w", encoding="utf-8", newline="\n") as handle:
                write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
                write_record(
                    handle,
                    {
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call", "call_id": "old-call"},
                    },
                )
                write_record(
                    handle,
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "old-call",
                            "output": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64:" + "A" * 512,
                                }
                            ],
                        },
                    },
                )
                write_record(handle, {"type": "compacted", "payload": {"window_number": 1}})
                write_record(handle, {"type": "world_state", "payload": {"state": "one"}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})
                write_record(
                    handle,
                    {
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call", "call_id": "protected-call"},
                    },
                )
                write_record(
                    handle,
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "protected-call",
                            "output": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64:" + "B" * 512,
                                }
                            ],
                        },
                    },
                )
                write_record(handle, {"type": "compacted", "payload": {"window_number": 2}})
                write_record(handle, {"type": "world_state", "payload": {"state": "two"}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})

            plan = build_plan(source, root / "reports", recent_compactions=2)

            self.assertEqual(plan["protected_region"]["from_line"], 4)
            self.assertEqual(plan["protected_region"]["logical_compactions"], 2)
            candidate_lines = Path(plan["candidate_path"]).read_bytes().splitlines()
            self.assertIn(b"[image cache cleared]", candidate_lines[2])
            self.assertIn(b"data:image/png;base64", candidate_lines[7])

    def test_recent_compactions_boundary_is_rechecked_by_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            with source.open("w", encoding="utf-8", newline="\n") as handle:
                write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
                write_record(handle, {"type": "compacted", "payload": {"window_number": 1}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})
                write_record(handle, {"type": "compacted", "payload": {"window_number": 2}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})

            plan = build_plan(source, root / "reports", recent_compactions=2)
            audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(audit["status"], "pass")

            plan["protected_region"]["selected_boundary_lines"] = [999]
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )

            tampered_audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(tampered_audit["status"], "fail")
            self.assertTrue(
                any(
                    "protected boundary metadata mismatch" in error
                    for error in tampered_audit["errors"]
                )
            )

    def test_context_only_compactions_are_counted_as_logical_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            with source.open("w", encoding="utf-8", newline="\n") as handle:
                write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})

            plan = build_plan(source, root / "reports", recent_compactions=2)

            self.assertEqual(plan["protected_region"]["selected_boundary_lines"], [2, 3])
            self.assertEqual(plan["protected_region"]["reason"], "latest_logical_compactions")

    def test_mixed_compaction_records_pair_in_source_order(self):
        records = [
            {"type": "session_meta", "payload": {"id": "session-1"}},
            {"type": "event_msg", "payload": {"type": "context_compacted"}},
            {"type": "compacted", "payload": {"window_number": 1}},
            {"type": "response_item", "payload": {"type": "context_compacted"}},
            {"type": "compacted", "payload": {"window_number": 2}},
            {"type": "event_msg", "payload": {"type": "context_compacted"}},
        ]

        self.assertEqual(logical_compaction_boundaries(records), [2, 3, 5])

    def test_recent_compactions_uses_all_available_boundaries_when_requested_count_is_larger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            with source.open("w", encoding="utf-8", newline="\n") as handle:
                write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
                write_record(handle, {"type": "compacted", "payload": {"window_number": 1}})
                write_record(handle, {"type": "event_msg", "payload": {"type": "context_compacted"}})

            plan = build_plan(source, root / "reports", recent_compactions=3)
            region = plan["protected_region"]

            self.assertEqual(region["from_line"], 2)
            self.assertEqual(region["selected_boundary_lines"], [2])
            self.assertEqual(region["logical_compactions"], 1)
            self.assertEqual(region["requested_logical_compactions"], 3)
            self.assertEqual(region["available_logical_compactions"], 1)
            self.assertEqual(region["reason"], "available_logical_compactions")

    def test_recent_compactions_rejects_non_positive_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "session.jsonl"
            make_session(source)

            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", recent_compactions=0)
            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", recent_compactions=-1)

    def test_audit_stages_have_bound_digests_and_apply_requires_all_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(
                [stage["name"] for stage in audit["stages"]],
                ["schema", "policy", "deterministic_transform", "integrity"],
            )
            for stage in audit["stages"]:
                self.assertEqual(stage["status"], "pass")
                self.assertIsInstance(stage["input_digest"], str)
                self.assertEqual(stage["result_digest"], self_digest(stage, "result_digest"))

            audit["stages"] = audit["stages"][:-1]
            audit["audit_digest"] = self_digest(audit, "audit_digest")
            Path(plan["audit_path"]).write_text(
                json.dumps(audit, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaises(ValueError):
                apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

    def test_apply_rejects_self_consistent_but_recomputed_stage_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))

            audit = json.loads(Path(plan["audit_path"]).read_text(encoding="utf-8"))
            audit["stages"][0]["observations"]["source_records"] = 999
            audit["stages"][0]["result_digest"] = self_digest(
                audit["stages"][0], "result_digest"
            )
            audit["audit_digest"] = self_digest(audit, "audit_digest")
            Path(plan["audit_path"]).write_text(
                json.dumps(audit, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaises(ValueError):
                apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

    def test_plan_only_changes_old_tool_outputs_and_preserves_recent_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            make_session(source)

            plan = build_plan(source, report_dir)

            self.assertEqual(plan["status"], "ready_for_review")
            self.assertEqual(plan["summary"]["changed_records"], 2)
            self.assertEqual(plan["summary"]["image_payloads_cleared"], 1)
            self.assertEqual(plan["summary"]["truncated_outputs"], 1)
            self.assertGreater(plan["summary"]["bytes_saved"], 0)

            original_lines = source.read_bytes().splitlines(keepends=True)
            candidate_lines = Path(plan["candidate_path"]).read_bytes().splitlines(keepends=True)
            self.assertEqual(candidate_lines[-1], original_lines[-1])
            self.assertEqual(candidate_lines[1], original_lines[1])
            self.assertIn(b"[image cache cleared]", candidate_lines[3])
            self.assertIn(b"[older tool output middle truncated]", candidate_lines[5])

    def test_independent_audit_rejects_tampered_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            make_session(source)
            plan = build_plan(source, report_dir)

            candidate = Path(plan["candidate_path"])
            data = candidate.read_bytes().replace(b"Keep this", b"Changed user text")
            candidate.write_bytes(data)

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("protected" in error for error in audit["errors"]))

    def test_apply_requires_confirmation_and_creates_restorable_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))

            with self.assertRaises(ValueError):
                apply_plan(Path(plan["report_path"]), "wrong-confirmation", backup_root)

            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

            self.assertEqual(result["status"], "success")
            self.assertTrue(Path(result["backup_path"]).exists())
            self.assertNotIn(b"data:image/png;base64", source.read_bytes())
            self.assertIn(b"Keep this", source.read_bytes())

            backups = list_backups(backup_root, session_id="session-1")
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0]["status"], "success")

            restored_hash = restore_backup(Path(result["backup_path"]), result["backup_id"])
            self.assertEqual(restored_hash["status"], "success")
            self.assertEqual(source.read_bytes(), Path(result["backup_path"], "original.jsonl").read_bytes())

    def test_backup_listing_marks_corrupt_success_as_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups" / "session-1"
            batch = backup_root / "corrupt"
            batch.mkdir(parents=True)
            original = batch / "original.jsonl"
            original.write_bytes(b"not-json\n")
            (batch / "manifest.json").write_text(
                json.dumps(
                    {
                        "backup_id": "corrupt",
                        "session_id": "session-1",
                        "status": "success",
                        "created_at": "2026-08-13T00:00:00Z",
                        "source_path": str(root / "session.jsonl"),
                        "original_sha256": "wrong",
                    }
                ),
                encoding="utf-8",
            )

            entries = list_backups(backup_root.parent, session_id="session-1")

            self.assertEqual(entries[0]["status"], "success")
            self.assertEqual(entries[0]["integrity"], "invalid")
            self.assertIn("mismatch", entries[0]["integrity_error"])

    def test_apply_rejects_plan_tampering_after_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))

            plan["review_note"] = "tampered after audit"
            Path(plan["report_path"]).write_text(json.dumps(plan), encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

    def test_restore_rejects_an_alternate_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            report_dir = root / "reports"
            backup_root = root / "backups"
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

            with self.assertRaises(ValueError):
                restore_backup(Path(result["backup_path"]), result["backup_id"], root / "other.jsonl")

    def test_restore_rolls_back_when_post_replace_validation_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)
            current_content = source.read_bytes()
            original_content = Path(result["backup_path"], "original.jsonl").read_bytes()

            real_sha256_file = session_cleanup.sha256_file

            def fail_after_replace(path):
                resolved = Path(path).resolve()
                if resolved == source.resolve() and source.read_bytes() == original_content:
                    return "forced-post-replace-hash-failure"
                return real_sha256_file(path)

            with mock.patch.object(session_cleanup, "sha256_file", side_effect=fail_after_replace):
                with self.assertRaises(ValueError):
                    restore_backup(Path(result["backup_path"]), result["backup_id"])

            self.assertEqual(source.read_bytes(), current_content)

    def test_backup_listing_exposes_incomplete_batches_as_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir) / "backups" / "session-1"
            incomplete = backup_root / "incomplete"
            incomplete.mkdir(parents=True)

            entries = list_backups(backup_root.parent, session_id="session-1")

            self.assertEqual(entries[0]["status"], "unknown")
            self.assertIn("manifest", entries[0]["reason"])

    def test_backup_listing_ignores_internal_preview_and_quarantine_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir) / "backups"
            (backup_root / ".prune-previews").mkdir(parents=True)
            (backup_root / ".prune-quarantine").mkdir(parents=True)

            self.assertEqual(list_backups(backup_root), [])

    def test_backup_listing_rejects_path_traversal_session_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir) / "backups"

            with self.assertRaises(ValueError):
                list_backups(backup_root, session_id="..")
            with self.assertRaises(ValueError):
                list_backups(backup_root, session_id="nested/session")

    def test_prune_preserves_corrupt_success_backup_and_binds_confirmation_to_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups" / "session-1"
            backup_root.mkdir(parents=True)
            valid_content = b'{"type":"session_meta","payload":{"id":"session-1"}}\n'
            for index in range(3):
                batch = backup_root / f"batch-{index}"
                batch.mkdir()
                original = batch / "original.jsonl"
                original.write_bytes(
                    valid_content
                    + json.dumps({"type": "event_msg", "payload": {"index": index}}).encode()
                    + b"\n"
                )

                write_manifest(
                    batch / "manifest.json",
                    {
                        "backup_version": 2,
                        "backup_id": f"batch-{index}",
                        "session_id": "session-1",
                        "status": "success",
                        "created_at": f"2026-08-13T00:00:0{index}Z",
                        "source_path": str(root / "session.jsonl"),
                        "original_sha256": sha256_file(original),
                    },
                )
            corrupt = backup_root / "corrupt"
            corrupt.mkdir()
            (corrupt / "original.jsonl").write_bytes(b"not-json\n")
            (corrupt / "manifest.json").write_text(
                json.dumps(
                    {
                        "backup_id": "corrupt",
                        "session_id": "session-1",
                        "status": "success",
                        "created_at": "2026-08-13T00:00:09Z",
                        "source_path": str(root / "session.jsonl"),
                        "original_sha256": "wrong",
                    }
                ),
                encoding="utf-8",
            )

            preview = prune_backups(backup_root.parent, "session-1", keep=2, confirm=None)
            self.assertEqual(preview["delete_count"], 1)
            self.assertIn(str(corrupt.resolve()), preview["preserved_paths"])

            shutil.copytree(backup_root / "batch-2", backup_root / "new-success")
            new_manifest = json.loads((backup_root / "new-success" / "manifest.json").read_text(encoding="utf-8"))
            new_manifest["backup_id"] = "new-success"
            new_manifest["created_at"] = "2026-08-13T00:00:10Z"
            write_manifest(backup_root / "new-success" / "manifest.json", new_manifest)
            with self.assertRaises(ValueError):
                prune_backups(backup_root.parent, "session-1", keep=2, confirm=preview["preview_id"])

            refreshed_preview = prune_backups(backup_root.parent, "session-1", keep=2, confirm=None)
            result = prune_backups(
                backup_root.parent,
                "session-1",
                keep=2,
                confirm=refreshed_preview["preview_id"],
            )
            self.assertEqual(result["status"], "success")

    def test_plan_rejects_dangerous_thresholds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "session.jsonl"
            make_session(source)
            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", recent_records=0)
            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", max_output_bytes=-1)
            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", prefix_bytes=0)
            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", suffix_bytes=0)

    def test_visible_role_on_tool_output_is_never_transformed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            with source.open("w", encoding="utf-8", newline="\n") as handle:
                write_record(handle, {"type": "session_meta", "payload": {"id": "session-1"}})
                write_record(
                    handle,
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "role": "assistant",
                            "call_id": "visible-output",
                            "output": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64:" + "B" * 512,
                                }
                            ],
                        },
                    },
                )
                write_record(handle, {"type": "event_msg", "payload": {"text": "recent"}})

            plan = build_plan(source, root / "reports", recent_records=1)

            self.assertEqual(plan["summary"]["changed_records"], 0)
            self.assertEqual(Path(plan["candidate_path"]).read_bytes(), source.read_bytes())

    def test_audit_rejects_candidate_that_does_not_match_declared_transform(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            make_session(source)
            plan = build_plan(source, report_dir)
            candidate = Path(plan["candidate_path"])
            candidate.write_bytes(candidate.read_bytes().replace(b"[image cache cleared]", b"arbitrary replacement"))

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("deterministic" in error for error in audit["errors"]))

    def test_prune_keeps_two_successful_backups_and_never_deletes_failed_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups" / "session-1"
            backup_root.mkdir(parents=True)
            for index, status in enumerate(("success", "success", "success", "failed")):
                batch = backup_root / f"batch-{index}"
                batch.mkdir()
                original = batch / "original.jsonl"
                original.write_text(
                    json.dumps({"type": "session_meta", "payload": {"id": "session-1", "index": index}}) + "\n",
                    encoding="utf-8",
                )
                write_manifest(
                    batch / "manifest.json",
                    {
                        "backup_version": 2,
                        "backup_id": f"batch-{index}",
                        "session_id": "session-1",
                        "status": status,
                        "created_at": f"2026-08-13T00:00:0{index}Z",
                        "source_path": str(root / "session.jsonl"),
                        "original_sha256": sha256_file(original),
                    },
                )

            preview = prune_backups(backup_root.parent, "session-1", keep=2, confirm=False)
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["delete_count"], 1)

            result = prune_backups(backup_root.parent, "session-1", keep=2, confirm=preview["preview_id"])
            self.assertEqual(result["status"], "success")
            self.assertFalse((backup_root / "batch-0").exists())
            self.assertTrue((backup_root / "batch-1").exists())
            self.assertTrue((backup_root / "batch-3").exists())
            quarantine_root = backup_root.parent / ".prune-quarantine"
            self.assertTrue(quarantine_root.exists())
            self.assertEqual(list(quarantine_root.iterdir()), [])

    def test_prune_restores_moved_batches_when_quarantine_move_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups" / "session-1"
            backup_root.mkdir(parents=True)
            for index in range(3):
                batch = backup_root / f"batch-{index}"
                batch.mkdir()
                original = batch / "original.jsonl"
                original.write_text(
                    json.dumps({"type": "session_meta", "payload": {"id": "session-1"}}) + "\n",
                    encoding="utf-8",
                )
                write_manifest(
                    batch / "manifest.json",
                    {
                        "backup_version": 2,
                        "backup_id": f"batch-{index}",
                        "session_id": "session-1",
                        "status": "success",
                        "created_at": f"2026-08-13T00:00:0{index}Z",
                        "source_path": str(root / "session.jsonl"),
                        "original_sha256": sha256_file(original),
                    },
                )

            preview = prune_backups(backup_root.parent, "session-1", keep=1)
            real_replace = session_cleanup.os.replace
            calls = 0

            def fail_on_second_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("forced quarantine move failure")
                return real_replace(source, destination)

            with mock.patch.object(session_cleanup.os, "replace", side_effect=fail_on_second_move):
                with self.assertRaises(RuntimeError):
                    prune_backups(
                        backup_root.parent,
                        "session-1",
                        keep=1,
                        confirm=preview["preview_id"],
                    )

            self.assertTrue((backup_root / "batch-0").exists())
            self.assertTrue((backup_root / "batch-1").exists())
            self.assertTrue((backup_root / "batch-2").exists())

    def test_restore_rejects_tampered_manifest_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)
            manifest_path = Path(result["backup_path"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_path"] = str(root / "other.jsonl")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(ValueError):
                restore_backup(Path(result["backup_path"]), result["backup_id"])


if __name__ == "__main__":
    unittest.main()
