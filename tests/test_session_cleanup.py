import json
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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


def make_target_session(path):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        write_record(handle, {"type": "session_meta", "payload": {"id": "target-session"}})
        for index in range(4):
            call_id = f"target-call-{index}"
            write_record(
                handle,
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "call_id": call_id},
                },
            )
            write_record(
                handle,
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": [{"type": "input_text", "text": "x" * 100000}],
                    },
                },
            )
        write_record(handle, {"type": "compacted", "payload": {"summary": "target boundary"}})


def _semantic_fixture(temp_dir, operation_count=1):
    """Create source-bound text-only semantic cleanup operations."""
    source = Path(temp_dir) / "semantic-session.jsonl"
    first_records = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": f"call-{index + 1}",
                "output": [
                    {
                        "type": "input_text",
                        "text": "old output" if index == 0 else f"old output {index + 1}",
                    }
                ],
            },
        }
        for index in range(operation_count)
    ]
    first_lines = [
        (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        for record in first_records
    ]
    filler_lines = [
        (
            json.dumps(
                {"type": "event_msg", "payload": {"text": f"filler-{index}"}},
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for index in range(1000)
    ]
    raw = b"".join([*first_lines, *filler_lines])
    source.write_bytes(raw)
    records, raw_lines, errors = session_cleanup.parse_jsonl(source)
    assert errors == []
    operations = []
    blocks = []
    for index, record in enumerate(first_records):
        line = index + 1
        rendered_text = (
            "[session cleanup capsule]\nretained: old output"
            if index == 0
            else f"[session cleanup capsule]\nretained: old output {index + 1}"
        )
        blocks.append({"block_id": f"b-{index + 1}", "source_lines": [line, line], "role": "context"})
        operations.append(
            {
                "block_id": f"b-{index + 1}",
                "line": line,
                "record_index": index,
                "call_id": record["payload"]["call_id"],
                "json_pointer": "/payload/output",
                "source_node_sha256": session_cleanup.hash_json_node(record["payload"]["output"]),
                "rendered_text": rendered_text,
            }
        )
    sidecar = {"capsule_id": "capsule-1"}
    if operation_count == 1:
        sidecar["rendered_text"] = operations[0]["rendered_text"]
    bundle = {
        "semantic_map_version": 1,
        "source": {"sha256": sha256_file(source), "bytes": len(raw)},
        "blocks": blocks,
        "operations": operations,
        "sidecar": sidecar,
        "semantic_review": {
            "planner": {"artifact": "planner-review-1", "status": "pass"},
            "critic": {"artifact": "critic-review-1", "status": "pass"},
            "independent": True,
            "disagreements": [],
        },
    }
    bundle_path = Path(temp_dir) / "semantic-bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return {
        "source": source,
        "records": records,
        "raw_lines": raw_lines,
        "protected_from": 99,
        "bundle": bundle,
        "bundle_path": bundle_path,
    }


def _semantic_plan_fixture(temp_dir):
    root = Path(temp_dir)
    fixture = _semantic_fixture(root)
    plan = session_cleanup.build_semantic_plan(
        fixture["source"], root / "reports", fixture["bundle_path"], 1000, 2
    )
    return {
        **fixture,
        "root": root,
        "plan": plan,
        "plan_path": Path(plan["report_path"]),
        "sidecar_path": Path(plan["sidecar"]["path"]),
        "backup_root": root / "backups",
    }


def make_backup_batch(backup_root, session_id, batch_id, created_at):
    batch = Path(backup_root) / session_id / batch_id
    batch.mkdir(parents=True)
    original = batch / "original.jsonl"
    original.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "batch": batch_id}}) + "\n",
        encoding="utf-8",
    )
    write_manifest(
        batch / "manifest.json",
        {
            "backup_version": 1,
            "backup_id": batch_id,
            "session_id": session_id,
            "status": "success",
            "created_at": created_at,
            "source_path": str(Path(backup_root).parent / f"{session_id}.jsonl"),
            "original_sha256": sha256_file(original),
        },
    )
    return batch


class SessionCleanupTests(unittest.TestCase):
    def test_semantic_bundle_accepts_source_bound_text_operation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))

            self.assertEqual(
                session_cleanup.validate_semantic_bundle(
                    fixture["bundle"],
                    fixture["records"],
                    fixture["raw_lines"],
                    fixture["protected_from"],
                ),
                [],
            )

    def test_semantic_bundle_rejects_visible_and_protected_operations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))

            errors = session_cleanup.validate_semantic_bundle(
                fixture["bundle"], fixture["records"], fixture["raw_lines"], 1
            )

            self.assertIn("protected_or_visible_record", errors)

    def test_semantic_bundle_rejects_structured_output_and_wrong_pointer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            structured_records = copy.deepcopy(fixture["records"])
            structured_records[0]["payload"]["output"].append(
                {
                    "type": "input_image",
                    "image_url": "https://example.invalid/image.png",
                }
            )

            errors = session_cleanup.validate_semantic_bundle(
                fixture["bundle"],
                structured_records,
                fixture["raw_lines"],
                fixture["protected_from"],
            )

            self.assertTrue(errors)

    def test_semantic_bundle_rejects_stale_call_id_or_node_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            stale_bundle = copy.deepcopy(fixture["bundle"])
            stale_bundle["operations"][0]["call_id"] = "call-stale"

            errors = session_cleanup.validate_semantic_bundle(
                stale_bundle,
                fixture["records"],
                fixture["raw_lines"],
                fixture["protected_from"],
            )

            self.assertIn("source_identity_mismatch", errors)

    def test_semantic_bundle_rejects_malformed_source_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            malformed_bundle = copy.deepcopy(fixture["bundle"])
            malformed_bundle["operations"] = []
            malformed_records = copy.deepcopy(fixture["records"])
            malformed_records[0] = {"__invalid__": True}

            errors = session_cleanup.validate_semantic_bundle(
                malformed_bundle,
                malformed_records,
                fixture["raw_lines"],
                fixture["protected_from"],
            )

            self.assertIn("malformed_source_record", errors)

    def test_semantic_bundle_rejects_source_length_mismatch_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))

            errors = session_cleanup.validate_semantic_bundle(
                fixture["bundle"], fixture["records"], [], fixture["protected_from"]
            )

            self.assertIn("source_length_mismatch", errors)

    def test_semantic_bundle_rejects_invalid_boundary_and_block_id_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            invalid_block_bundle = copy.deepcopy(fixture["bundle"])
            invalid_block_bundle["operations"][0]["block_id"] = []

            invalid_boundary_errors = session_cleanup.validate_semantic_bundle(
                fixture["bundle"],
                fixture["records"],
                fixture["raw_lines"],
                None,
            )
            invalid_block_errors = session_cleanup.validate_semantic_bundle(
                invalid_block_bundle,
                fixture["records"],
                fixture["raw_lines"],
                fixture["protected_from"],
            )

            self.assertIn("invalid_protected_boundary", invalid_boundary_errors)
            self.assertIn("invalid_block_id", invalid_block_errors)

    def test_semantic_bundle_rejects_non_string_source_call_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            record = copy.deepcopy(fixture["records"][0])
            record["payload"]["call_id"] = 1
            raw_line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            bundle = copy.deepcopy(fixture["bundle"])
            bundle["source"] = {
                "sha256": session_cleanup.sha256_bytes(raw_line),
                "bytes": len(raw_line),
            }
            bundle["operations"][0]["call_id"] = "1"

            errors = session_cleanup.validate_semantic_bundle(
                bundle, [record], [raw_line], fixture["protected_from"]
            )

            self.assertIn("missing_call_id", errors)

    def test_materialize_semantic_candidate_replaces_only_declared_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            candidate = Path(temp_dir) / "candidate.jsonl"

            result = session_cleanup.materialize_semantic_candidate(
                fixture["source"],
                candidate,
                fixture["bundle"],
                fixture["records"],
                fixture["raw_lines"],
                2,
            )
            candidate_records, candidate_lines, errors = session_cleanup.parse_jsonl(candidate)

            self.assertEqual(result["changed_records"], 1)
            self.assertEqual(errors, [])
            self.assertEqual(candidate_records[0]["payload"]["call_id"], "call-1")
            self.assertEqual(
                candidate_records[0]["payload"]["output"],
                [{"type": "input_text", "text": fixture["bundle"]["operations"][0]["rendered_text"]}],
            )
            self.assertEqual(candidate_lines[1:], fixture["raw_lines"][1:])

    def test_materialize_semantic_candidate_preserves_classic_mac_line_ending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_fixture(Path(temp_dir))
            fixture["source"].write_bytes(fixture["source"].read_bytes().replace(b"\n", b"\r"))
            records, raw_lines, errors = session_cleanup.parse_jsonl(fixture["source"])
            self.assertEqual(errors, [])
            fixture["records"] = records
            fixture["raw_lines"] = raw_lines
            fixture["bundle"]["source"] = {
                "sha256": sha256_file(fixture["source"]),
                "bytes": fixture["source"].stat().st_size,
            }
            candidate = Path(temp_dir) / "candidate-cr.jsonl"

            session_cleanup.materialize_semantic_candidate(
                fixture["source"],
                candidate,
                fixture["bundle"],
                records,
                raw_lines,
                2,
            )

            candidate_bytes = candidate.read_bytes()
            first_candidate_line, first_separator, _ = candidate_bytes.partition(b"\r")
            self.assertEqual(first_separator, b"\r")
            self.assertIn(b"retained: old output", first_candidate_line)

    def test_semantic_plan_contains_sidecar_and_v4_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root)

            plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "reports", fixture["bundle_path"], 1000, 2
            )

            self.assertEqual(plan["plan_version"], 4)
            self.assertEqual(plan["audit_version"], 3)
            self.assertEqual(plan["candidate_kind"], "semantic_cleanup")
            self.assertEqual(plan["status"], "ready_for_review")
            self.assertIn("semantic_map_digest", plan)
            self.assertIn("sidecar", plan)
            self.assertTrue(Path(plan["sidecar"]["path"]).is_file())
            self.assertEqual(plan["summary"]["changed_records"], 1)

    def test_semantic_plan_has_no_change_when_no_safe_operation_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root)
            unsafe_bundle = copy.deepcopy(fixture["bundle"])
            unsafe_bundle["operations"] = []
            unsafe_bundle_path = root / "unsafe-bundle.json"
            unsafe_bundle_path.write_text(json.dumps(unsafe_bundle), encoding="utf-8")

            plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "reports", unsafe_bundle_path, 1000, 2
            )

            self.assertIn(plan["status"], {"no_change", "blocked"})
            self.assertEqual(plan["summary"]["changed_records"], 0)

    def test_semantic_plan_preserves_visible_user_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root)
            source_lines = fixture["source"].read_bytes().splitlines(keepends=True)
            visible_line = (
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64:" + "C" * 128,
                                }
                            ],
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            fixture["source"].write_bytes(source_lines[0] + visible_line + b"".join(source_lines[1:]))

            bundle = copy.deepcopy(fixture["bundle"])
            bundle["source"] = {
                "sha256": sha256_file(fixture["source"]),
                "bytes": fixture["source"].stat().st_size,
            }
            fixture["bundle_path"].write_text(json.dumps(bundle), encoding="utf-8")
            plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "reports", fixture["bundle_path"], 1000, 2
            )

            self.assertEqual(plan["status"], "ready_for_review")
            self.assertEqual(plan["summary"]["changed_records"], 1)
            candidate_lines = Path(plan["candidate_path"]).read_bytes().splitlines(keepends=True)
            self.assertEqual(candidate_lines[1], visible_line)
            self.assertEqual(session_cleanup.audit_plan(Path(plan["report_path"]))["status"], "pass")

    def test_semantic_plan_audits_multiple_operations_in_source_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root, operation_count=2)
            plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "reports", fixture["bundle_path"], 1000, 2
            )

            self.assertEqual(plan["status"], "ready_for_review")
            self.assertEqual(plan["summary"]["changed_records"], 2)
            self.assertEqual([item["line"] for item in plan["transformation"]["changed_lines"]], [1, 2])
            audit = session_cleanup.audit_plan(Path(plan["report_path"]))
            self.assertEqual(audit["status"], "pass")

    def test_semantic_apply_rejects_wrong_confirmation_and_no_change_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_plan_fixture(root)
            self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")

            with self.assertRaisesRegex(ValueError, "confirmation must exactly equal plan_id"):
                session_cleanup.apply_plan(fixture["plan_path"], "not-the-plan-id", fixture["backup_root"])

            no_change_bundle = copy.deepcopy(fixture["bundle"])
            no_change_bundle["operations"] = []
            no_change_path = root / "no-change-bundle.json"
            no_change_path.write_text(json.dumps(no_change_bundle), encoding="utf-8")
            no_change_plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "no-change-reports", no_change_path, 1000, 2
            )
            self.assertEqual(no_change_plan["status"], "no_change")
            with self.assertRaisesRegex(ValueError, "semantic plan is not ready for review"):
                session_cleanup.apply_plan(
                    Path(no_change_plan["report_path"]),
                    no_change_plan["plan_id"],
                    fixture["backup_root"],
                )

    def test_semantic_plan_cli_route_returns_reviewable_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root)
            command = [sys.executable, str(SCRIPT_DIR / "session_cleanup.py")]
            process = subprocess.run(
                command
                + [
                    "semantic-plan",
                    str(fixture["source"]),
                    "--bundle",
                    str(fixture["bundle_path"]),
                    "--report-dir",
                    str(root / "reports"),
                    "--recent-records",
                    "1000",
                    "--recent-compactions",
                    "2",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            plan = json.loads(process.stdout)
            self.assertEqual(plan["candidate_kind"], "semantic_cleanup")

    def test_semantic_audit_requires_ordered_v3_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))

            audit = session_cleanup.audit_plan(fixture["plan_path"])

            self.assertEqual(audit["status"], "pass")
            self.assertEqual(audit["audit_version"], 3)
            self.assertEqual(
                [stage["name"] for stage in audit["stages"]],
                ["schema", "semantic_review", "policy", "deterministic_transform", "integrity"],
            )

    def test_semantic_audit_rejects_changed_sidecar_or_rendered_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            fixture["sidecar_path"].write_bytes(fixture["sidecar_path"].read_bytes() + b"x")

            audit = session_cleanup.audit_plan(fixture["plan_path"])

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("sidecar" in error for error in audit["errors"]))

    def test_semantic_audit_rejects_reused_planner_and_critic_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = _semantic_fixture(root)
            bundle = copy.deepcopy(fixture["bundle"])
            bundle["semantic_review"]["critic"]["artifact"] = bundle["semantic_review"]["planner"]["artifact"]
            bundle_path = root / "same-review-artifact-bundle.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            plan = session_cleanup.build_semantic_plan(
                fixture["source"], root / "reports", bundle_path, 1000, 2
            )

            audit = session_cleanup.audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("artifacts must be distinct" in error for error in audit["errors"]))

    def test_semantic_apply_binds_backup_id_without_changing_reviewed_capsule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            audit = session_cleanup.audit_plan(fixture["plan_path"])
            self.assertEqual(audit["status"], "pass")
            sidecar_before = fixture["sidecar_path"].read_bytes()
            candidate_sha256 = fixture["plan"]["candidate"]["sha256"]

            result = session_cleanup.apply_plan(
                fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(fixture["sidecar_path"].read_bytes(), sidecar_before)
            self.assertEqual(session_cleanup.sha256_file(fixture["source"]), candidate_sha256)
            manifest = json.loads(
                (Path(result["backup_path"]) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["backup_id"], result["backup_id"])
            self.assertEqual(manifest["sidecar_sha256"], fixture["plan"]["sidecar"]["sha256"])

    def test_semantic_apply_rejects_stale_source_or_missing_sidecar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")
            fixture["source"].write_bytes(fixture["source"].read_bytes() + b"\n")

            with self.assertRaises(ValueError):
                session_cleanup.apply_plan(
                    fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")
            fixture["sidecar_path"].unlink()

            with self.assertRaises(ValueError):
                session_cleanup.apply_plan(
                    fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
                )

    def test_semantic_reconciliation_reports_verified_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")
            result = session_cleanup.apply_plan(
                fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
            )

            state = session_cleanup.reconcile_apply_batch(Path(result["backup_path"]))

            self.assertEqual(state["status"], "success")

    def test_semantic_reconciliation_rejects_tampered_batch_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _semantic_plan_fixture(Path(temp_dir))
            self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")
            result = session_cleanup.apply_plan(
                fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
            )
            batch = Path(result["backup_path"])
            manifest_path = batch / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["backup_version"] = 99
            manifest["session_id"] = "different-session"
            write_manifest(manifest_path, manifest)

            state = session_cleanup.reconcile_apply_batch(batch)

            self.assertEqual(state["status"], "needs_manual_recovery")

    def test_semantic_backup_listing_preserves_missing_staged_artifacts(self):
        for missing_name in ("candidate.jsonl", "sidecar.json"):
            with self.subTest(missing_name=missing_name), tempfile.TemporaryDirectory() as temp_dir:
                fixture = _semantic_plan_fixture(Path(temp_dir))
                self.assertEqual(session_cleanup.audit_plan(fixture["plan_path"])["status"], "pass")
                result = session_cleanup.apply_plan(
                    fixture["plan_path"], fixture["plan"]["plan_id"], fixture["backup_root"]
                )
                (Path(result["backup_path"]) / missing_name).unlink()

                entries = session_cleanup.list_backups(
                    fixture["backup_root"], session_id=fixture["plan"]["session_id"]
                )

                self.assertEqual(entries[0]["status"], "success")
                self.assertEqual(entries[0]["integrity"], "invalid")
                self.assertFalse(entries[0]["deletion_eligible"])

    def test_profiles_have_expected_thresholds(self):
        self.assertEqual(session_cleanup.DEFAULT_RECENT_COMPACTIONS, 2)
        self.assertIsNone(session_cleanup.PROFILE_POLICIES["cache"]["max_output_bytes"])
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["max_output_bytes"], 64 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["prefix_bytes"], 8 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["balanced"]["suffix_bytes"], 4 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["max_output_bytes"], 16 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["prefix_bytes"], 2 * 1024)
        self.assertEqual(session_cleanup.PROFILE_POLICIES["space"]["suffix_bytes"], 1 * 1024)

    def test_public_skill_documents_semantic_workflow(self):
        root = Path(__file__).resolve().parents[1]
        skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
        readme_text = (root / "README.md").read_text(encoding="utf-8")
        schema_text = (root / "references" / "jsonl-schema.md").read_text(encoding="utf-8")

        for phrase in (
            "semantic bundle",
            "semantic-plan",
            "independent critic",
            "Understand",
            "Group",
            "Compare",
            "Confirm",
            "exact `plan_id`",
        ):
            self.assertIn(phrase, skill_text)
        for phrase in (
            "semantic-plan",
            "semantic_cleanup",
            "audit_version: 3",
            "sidecar",
            "legacy",
        ):
            self.assertIn(phrase, readme_text)
        for phrase in ("backup_verified", "sidecar_staged", "needs_manual_recovery"):
            self.assertIn(phrase, schema_text)

    def test_named_profile_rejects_manual_thresholds(self):
        with self.assertRaises(ValueError):
            session_cleanup.resolve_profile_policy("balanced", 4096, None, None)

    def test_truncate_output_keeps_utf8_boundaries(self):
        value = [{"type": "input_text", "text": "中文内容" * 500}]

        truncated, did_truncate = session_cleanup.truncate_output(value, 1024, 31, 29)

        self.assertTrue(did_truncate)
        encoded = json.dumps(truncated, ensure_ascii=False).encode("utf-8")
        self.assertNotIn(b"\xef\xbf\xbd", encoded)
        self.assertIn(b"[older tool output middle truncated]", encoded)

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

    def test_audit_rejects_missing_residual_risk_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan = build_plan(source, root / "reports")
            plan.pop("residual_risk")
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )

            audit = audit_plan(Path(plan["report_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("residual risk" in error for error in audit["errors"]))

    def test_plan_set_has_independent_profiles_and_intent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            result = session_cleanup.build_plan_set(
                source,
                root / "reports",
                profiles=["cache", "balanced", "space"],
                intent_profile={
                    "problem": "overall_size",
                    "retention_priority": "recent_content",
                    "allowed_strength": "balanced",
                    "target_bytes": None,
                    "assumptions": [],
                    "evidence": {"source": "test"},
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
            for entry in result["candidates"]:
                plan = json.loads(Path(entry["plan_path"]).read_text(encoding="utf-8"))
                self.assertEqual(entry["status"], plan["status"])
                self.assertEqual(entry["original_bytes"], plan["summary"]["original_bytes"])
                self.assertEqual(entry["protected_region"], plan["protected_region"])
                self.assertEqual(entry["residual_risk"], plan["residual_risk"])
            self.assertTrue(Path(result["plan_set_path"]).is_file())

    def test_audit_plan_set_audits_each_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan_set = session_cleanup.build_plan_set(source, root / "reports")

            audit = session_cleanup.audit_plan_set(Path(plan_set["plan_set_path"]))

            self.assertEqual(audit["status"], "pass")
            self.assertEqual(len(audit["candidate_audits"]), len(plan_set["candidates"]))
            self.assertTrue(all(item["status"] == "pass" for item in audit["candidate_audits"]))

    def test_apply_rejects_plan_set_and_old_plan_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan_set = session_cleanup.build_plan_set(source, root / "reports")

            with self.assertRaises(ValueError):
                apply_plan(Path(plan_set["plan_set_path"]), plan_set["plan_set_id"], root / "backups")

            old_plan_path = root / "old-plan.json"
            old_plan_path.write_text(
                json.dumps({"plan_version": 2, "plan_id": "old"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                apply_plan(old_plan_path, "old", root / "backups")

    def test_audit_plan_set_rejects_tampered_index_bindings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan_set = session_cleanup.build_plan_set(source, root / "reports")
            plan_set["candidates"][0]["source_sha256"] = "0" * 64
            plan_set["candidates"][0]["audit_path"] = str(root / "wrong-audit.json")
            plan_set["plan_set_digest"] = self_digest(plan_set, "plan_set_digest")
            Path(plan_set["plan_set_path"]).write_text(
                json.dumps(plan_set, ensure_ascii=False), encoding="utf-8"
            )

            audit = session_cleanup.audit_plan_set(Path(plan_set["plan_set_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("index" in error for error in audit["errors"]))

    def test_audit_plan_set_rejects_duplicate_requested_profile_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan_set = session_cleanup.build_plan_set(source, root / "reports")
            plan_set["requested_profiles"] = ["cache"]
            plan_set["plan_set_digest"] = self_digest(plan_set, "plan_set_digest")
            Path(plan_set["plan_set_path"]).write_text(
                json.dumps(plan_set, ensure_ascii=False), encoding="utf-8"
            )

            audit = session_cleanup.audit_plan_set(Path(plan_set["plan_set_path"]))

            self.assertEqual(audit["status"], "fail")
            self.assertTrue(any("profile" in error for error in audit["errors"]))

    def test_apply_rejects_incomplete_intent_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            make_session(source)
            plan = build_plan(source, root / "reports", intent_profile={})
            audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(audit["status"], "fail")

            with self.assertRaises(ValueError):
                apply_plan(Path(plan["report_path"]), plan["plan_id"], root / "backups")

    def test_target_between_profiles_uses_deterministic_custom_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "target.jsonl"
            make_target_session(source)
            balanced = build_plan(source, root / "balanced", profile="balanced")
            space = build_plan(source, root / "space", profile="space")
            target_bytes = (balanced["summary"]["candidate_bytes"] + space["summary"]["candidate_bytes"]) // 2

            plan = build_plan(
                source,
                root / "target-plan",
                profile="target",
                target_bytes=target_bytes,
            )
            repeated = build_plan(
                source,
                root / "target-plan-repeat",
                profile="target",
                target_bytes=target_bytes,
            )

            self.assertEqual(plan["status"], "ready_for_review")
            self.assertEqual(plan["policy"]["profile"], "custom")
            self.assertEqual(plan["target"]["target_bytes"], target_bytes)
            self.assertEqual(
                plan["target"]["selection_method"],
                "deterministic_scan_between_balanced_and_space",
            )
            self.assertEqual(
                (
                    plan["policy"]["max_output_bytes"],
                    plan["policy"]["prefix_bytes"],
                    plan["policy"]["suffix_bytes"],
                ),
                (
                    repeated["policy"]["max_output_bytes"],
                    repeated["policy"]["prefix_bytes"],
                    repeated["policy"]["suffix_bytes"],
                ),
            )
            self.assertLessEqual(plan["summary"]["candidate_bytes"], target_bytes)
            audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(audit["status"], "pass")

            plan["requested_profile"] = "balanced"
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )
            downgraded_audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(downgraded_audit["status"], "fail")
            self.assertTrue(any("target" in error for error in downgraded_audit["errors"]))

            plan["requested_profile"] = "target"
            plan["target"]["target_bytes"] += 1
            plan["plan_digest"] = self_digest(plan, "plan_digest")
            Path(plan["report_path"]).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )
            tampered_audit = audit_plan(Path(plan["report_path"]))
            self.assertEqual(tampered_audit["status"], "fail")
            self.assertTrue(any("target" in error for error in tampered_audit["errors"]))

    def test_target_below_space_floor_is_infeasible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "target.jsonl"
            make_target_session(source)

            plan = build_plan(source, root / "reports", profile="target", target_bytes=1)

            self.assertEqual(plan["status"], "infeasible")
            self.assertGreater(plan["target"]["remaining_protected_bytes"], 0)

    def test_target_mode_requires_explicit_target_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "target.jsonl"
            make_target_session(source)

            with self.assertRaises(ValueError):
                build_plan(source, Path(temp_dir) / "reports", profile="balanced", target_bytes=1000)

    def test_backups_cleanup_age_filter_keeps_recent_and_invalid_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            session_id = "session-1"
            make_backup_batch(backup_root, session_id, "newest", "2026-08-13T00:00:00Z")
            make_backup_batch(backup_root, session_id, "second", "2026-07-01T00:00:00Z")
            make_backup_batch(backup_root, session_id, "old", "2026-06-01T00:00:00Z")
            make_backup_batch(backup_root, session_id, "invalid-time", "not-a-date")

            preview = session_cleanup.prune_backups(
                backup_root,
                session_id,
                keep=2,
                older_than_days=30,
                now=datetime(2026, 8, 14, tzinfo=timezone.utc),
            )

            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["retained_valid_successful"], 2)
            self.assertIn("reclaimable_bytes", preview)
            self.assertTrue(all(item["age_days"] >= 30 for item in preview["candidates"]))
            self.assertTrue(any("invalid timestamp" in reason for reason in preview["preserved_reasons"]))

    def test_backup_cleanup_confirmation_binds_age_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            session_id = "session-1"
            make_backup_batch(backup_root, session_id, "newest", "2026-08-13T00:00:00Z")
            make_backup_batch(backup_root, session_id, "old", "2026-06-01T00:00:00Z")
            preview = session_cleanup.prune_backups(
                backup_root,
                session_id,
                keep=1,
                older_than_days=30,
                now=datetime(2026, 8, 14, tzinfo=timezone.utc),
            )

            with self.assertRaises(ValueError):
                session_cleanup.prune_backups(
                    backup_root,
                    session_id,
                    keep=1,
                    older_than_days=0,
                    confirm=preview["preview_id"],
                )

    def test_backup_cleanup_confirmation_requires_preview_version_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            session_id = "session-1"
            make_backup_batch(backup_root, session_id, "newest", "2026-08-13T00:00:00Z")
            make_backup_batch(backup_root, session_id, "old", "2026-06-01T00:00:00Z")
            preview = session_cleanup.prune_backups(backup_root, session_id, keep=1)
            preview_path = backup_root / ".prune-previews" / f"{preview['preview_id']}.json"
            stored = json.loads(preview_path.read_text(encoding="utf-8"))
            stored["preview_version"] = 1
            stored["preview_digest"] = self_digest(stored, "preview_digest")
            preview_path.write_text(json.dumps(stored), encoding="utf-8")

            with self.assertRaises(ValueError):
                session_cleanup.prune_backups(
                    backup_root,
                    session_id,
                    keep=1,
                    confirm=preview["preview_id"],
                )

    def test_backup_cleanup_never_counts_directory_symlink_as_recovery_point(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            session_id = "session-1"
            make_backup_batch(backup_root, session_id, "newest", "2026-08-13T00:00:00Z")
            real_old = make_backup_batch(backup_root, session_id, "old", "2026-06-01T00:00:00Z")
            alias = backup_root / session_id / "old-alias"
            try:
                os.symlink(real_old, alias, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            preview = session_cleanup.prune_backups(backup_root, session_id, keep=1)

            self.assertIn(str(real_old.resolve()), preview["delete_paths"])
            self.assertNotIn(str(alias.absolute()), preview["delete_paths"])
            result = session_cleanup.prune_backups(
                backup_root,
                session_id,
                keep=1,
                confirm=preview["preview_id"],
            )
            self.assertEqual(result["status"], "success")
            self.assertTrue((backup_root / session_id / "newest").exists())
            self.assertTrue(alias.is_symlink())

    def test_cli_backup_cleanup_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            session_id = "session-1"
            make_backup_batch(backup_root, session_id, "newest", "2026-08-13T00:00:00Z")
            make_backup_batch(backup_root, session_id, "middle", "2026-07-01T00:00:00Z")
            make_backup_batch(backup_root, session_id, "old", "2026-06-01T00:00:00Z")
            command = [sys.executable, str(SCRIPT_DIR / "session_cleanup.py")]
            preview_process = subprocess.run(
                command
                + [
                    "backups",
                    "cleanup",
                    "--backup-root",
                    str(backup_root),
                    "--session-id",
                    session_id,
                    "--keep",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(preview_process.returncode, 0, preview_process.stderr)
            preview = json.loads(preview_process.stdout)
            self.assertEqual(preview["status"], "preview")
            self.assertIn("reclaimable_bytes", preview)
            self.assertIn("evaluation_now", preview)
            self.assertIn("retained_paths", preview)
            self.assertIn("snapshot", preview)
            self.assertIn("preview_digest", preview)
            self.assertIn("preview_path", preview)
            stored_preview = json.loads(Path(preview["preview_path"]).read_text(encoding="utf-8"))
            self.assertEqual(stored_preview["preview_digest"], preview["preview_digest"])
            self.assertEqual(stored_preview["snapshot"], preview["snapshot"])

            confirm_process = subprocess.run(
                command
                + [
                    "backups",
                    "cleanup",
                    "--backup-root",
                    str(backup_root),
                    "--session-id",
                    session_id,
                    "--keep",
                    "1",
                    "--confirm",
                    preview["preview_id"],
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(confirm_process.returncode, 0, confirm_process.stderr)
            self.assertEqual(json.loads(confirm_process.stdout)["status"], "success")

    def test_restore_rejects_backup_directory_outside_managed_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)

            untrusted = root / "untrusted" / "session-1" / result["backup_id"]
            untrusted.parent.mkdir(parents=True)
            shutil.copytree(result["backup_path"], untrusted)

            with self.assertRaises(ValueError):
                restore_backup(untrusted, result["backup_id"])

    def test_restore_rejects_symlinked_backup_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)
            batch = Path(result["backup_path"])
            real_is_symlink = Path.is_symlink

            def pretend_file_symlinks(path):
                if Path(path).resolve() in {batch / "manifest.json", batch / "original.jsonl"}:
                    return True
                return real_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=pretend_file_symlinks):
                with self.assertRaises(ValueError):
                    restore_backup(batch, result["backup_id"], backup_root=backup_root)
                listed = list_backups(backup_root, session_id="session-1")
            self.assertEqual(listed[0]["integrity"], "invalid")

    def test_cli_restore_accepts_explicit_managed_backup_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            backup_root = root / "backups"
            make_session(source)
            plan = build_plan(source, report_dir)
            audit_plan(Path(plan["report_path"]))
            result = apply_plan(Path(plan["report_path"]), plan["plan_id"], backup_root)
            command = [
                sys.executable,
                str(SCRIPT_DIR / "session_cleanup.py"),
                "restore",
                result["backup_path"],
                "--backup-root",
                str(backup_root),
                "--confirm",
                result["backup_id"],
            ]

            restore_process = subprocess.run(command, capture_output=True, text=True, check=False)

            self.assertEqual(restore_process.returncode, 0, restore_process.stderr)
            self.assertEqual(json.loads(restore_process.stdout)["status"], "success")

    def test_backup_listing_rejects_unknown_manifest_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backups"
            batch = make_backup_batch(backup_root, "session-1", "unknown-version", "2026-08-13T00:00:00Z")
            manifest_path = batch / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["backup_version"] = 99
            write_manifest(manifest_path, manifest)

            entries = list_backups(backup_root, session_id="session-1")

            self.assertEqual(entries[0]["integrity"], "invalid")
            self.assertIn("version", entries[0]["integrity_error"])
            with self.assertRaises(ValueError):
                restore_backup(batch, "unknown-version", backup_root=backup_root)

    def test_cli_candidate_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "session.jsonl"
            report_dir = root / "reports"
            make_session(source)
            command = [sys.executable, str(SCRIPT_DIR / "session_cleanup.py")]
            plan_process = subprocess.run(
                command
                + ["plan", str(source), "--report-dir", str(report_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(plan_process.returncode, 0, plan_process.stderr)
            plan_set = json.loads(plan_process.stdout)
            self.assertIsInstance(plan_set["plan_set_id"], str)
            self.assertGreaterEqual(len(plan_set["candidates"]), 1)
            self.assertIn("source", plan_set)

            audit_process = subprocess.run(
                command + ["audit", plan_set["plan_set_path"]],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(audit_process.returncode, 0, audit_process.stderr)
            audit = json.loads(audit_process.stdout)
            self.assertEqual(audit["status"], "pass")
            self.assertTrue(all(item["status"] == "pass" for item in audit["candidate_audits"]))

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

            restored_hash = restore_backup(
                Path(result["backup_path"]),
                result["backup_id"],
                backup_root=backup_root,
            )
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
                        "backup_version": 1,
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
                restore_backup(
                    Path(result["backup_path"]),
                    result["backup_id"],
                    root / "other.jsonl",
                    backup_root=backup_root,
                )

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
                    restore_backup(
                        Path(result["backup_path"]),
                        result["backup_id"],
                        backup_root=backup_root,
                    )

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
                        "backup_version": 1,
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
                        "backup_version": 1,
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
                        "backup_version": 1,
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
                        "backup_version": 1,
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
                restore_backup(
                    Path(result["backup_path"]),
                    result["backup_id"],
                    backup_root=backup_root,
                )


if __name__ == "__main__":
    unittest.main()
