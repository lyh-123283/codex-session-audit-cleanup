import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from session_cleanup import (  # noqa: E402
    apply_plan,
    audit_plan,
    build_plan,
    list_backups,
    prune_backups,
    restore_backup,
    sha256_file,
)


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

    def test_backup_listing_exposes_incomplete_batches_as_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_root = Path(temp_dir) / "backups" / "session-1"
            incomplete = backup_root / "incomplete"
            incomplete.mkdir(parents=True)

            entries = list_backups(backup_root.parent, session_id="session-1")

            self.assertEqual(entries[0]["status"], "unknown")
            self.assertIn("manifest", entries[0]["reason"])

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

                (batch / "manifest.json").write_text(
                    json.dumps(
                        {
                            "backup_id": f"batch-{index}",
                            "session_id": "session-1",
                            "status": "success",
                            "created_at": f"2026-08-13T00:00:0{index}Z",
                            "source_path": str(root / "session.jsonl"),
                            "original_sha256": sha256_file(original),
                        }
                    ),
                    encoding="utf-8",
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
            (backup_root / "new-success" / "manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
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
                (batch / "manifest.json").write_text(
                    json.dumps(
                        {
                            "backup_id": f"batch-{index}",
                            "session_id": "session-1",
                            "status": status,
                            "created_at": f"2026-08-13T00:00:0{index}Z",
                            "source_path": str(root / "session.jsonl"),
                            "original_sha256": sha256_file(original),
                        }
                    ),
                    encoding="utf-8",
                )

            preview = prune_backups(backup_root.parent, "session-1", keep=2, confirm=False)
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["delete_count"], 1)

            result = prune_backups(backup_root.parent, "session-1", keep=2, confirm=preview["preview_id"])
            self.assertEqual(result["status"], "success")
            self.assertFalse((backup_root / "batch-0").exists())
            self.assertTrue((backup_root / "batch-1").exists())
            self.assertTrue((backup_root / "batch-3").exists())


if __name__ == "__main__":
    unittest.main()
