#!/usr/bin/env python3
"""Conservative, plan-driven maintenance for one Codex JSONL session."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TOOL_CALL_TYPES = {"custom_tool_call", "function_call"}
TOOL_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output"}
VISIBLE_MESSAGE_TYPES = {"user_message", "agent_message"}
DEFAULT_RECENT_RECORDS = 1000
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_PREFIX_BYTES = 8 * 1024
DEFAULT_SUFFIX_BYTES = 4 * 1024
PLAN_VERSION = 1
AUDIT_STAGE_NAMES = ("schema", "policy", "deterministic_transform", "integrity")
BACKUP_INTERNAL_DIRS = {".prune-previews", ".prune-quarantine"}
RESERVED_NAMES = {
    "session_index.jsonl",
    "state_5.sqlite",
    "logs_2.sqlite",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def self_digest(value: dict[str, Any], field: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256_bytes(encoded)


def value_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256_bytes(encoded)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["manifest_digest"] = self_digest(manifest, "manifest_digest")
    write_json(path, manifest)


def fsync_directory(path: Path) -> None:
    """Best-effort directory durability; Windows may not expose directory fsync."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def copy_file_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    fsync_directory(destination.parent)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def payload_type(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    return str(payload.get("type", "")) if isinstance(payload, dict) else ""


def payload_role(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    return str(payload.get("role", "")) if isinstance(payload, dict) else ""


def is_compaction(record: dict[str, Any]) -> bool:
    return record.get("type") == "compacted" or payload_type(record) == "context_compacted"


def is_visible_message(record: dict[str, Any]) -> bool:
    kind = payload_type(record)
    return payload_role(record) in {"user", "assistant"} or kind in VISIBLE_MESSAGE_TYPES


def get_call_id(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("call_id")
    return str(value) if value is not None else None


def iter_nodes(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_nodes(child)


def count_image_nodes(value: Any) -> int:
    count = 0
    for node in iter_nodes(value):
        if (
            isinstance(node, dict)
            and node.get("type") == "input_image"
            and isinstance(node.get("image_url"), str)
            and node["image_url"].startswith("data:image")
        ):
            count += 1
    return count


def scrub_image_nodes(value: Any) -> tuple[Any, int]:
    if isinstance(value, list):
        result = []
        count = 0
        for child in value:
            scrubbed, child_count = scrub_image_nodes(child)
            result.append(scrubbed)
            count += child_count
        return result, count
    if isinstance(value, dict):
        if (
            value.get("type") == "input_image"
            and isinstance(value.get("image_url"), str)
            and value["image_url"].startswith("data:image")
        ):
            return {"type": "input_text", "text": "[image cache cleared]"}, 1
        result = {}
        count = 0
        for key, child in value.items():
            scrubbed, child_count = scrub_image_nodes(child)
            result[key] = scrubbed
            count += child_count
        return result, count
    return value, 0


def compact_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def truncate_output(value: Any, max_bytes: int, prefix_bytes: int, suffix_bytes: int) -> tuple[Any, bool]:
    encoded = compact_json_bytes(value)
    if len(encoded) <= max_bytes:
        return value, False
    prefix = encoded[:prefix_bytes].decode("utf-8", errors="replace") if prefix_bytes else ""
    suffix = encoded[-suffix_bytes:].decode("utf-8", errors="replace") if suffix_bytes else ""
    text = prefix + "\n[older tool output middle truncated]\n" + suffix
    if isinstance(value, list):
        return [{"type": "input_text", "text": text}], True
    if isinstance(value, dict):
        return {"type": "input_text", "text": text}, True
    return text, True


def parse_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[bytes], list[dict[str, Any]]]:
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        content = raw_line.rstrip(b"\r\n")
        if not content:
            errors.append({"line": line_number, "error": "empty_line"})
            records.append({"__invalid__": True})
            continue
        try:
            decoded = content.decode("utf-8")
            value = json.loads(decoded)
            if not isinstance(value, dict):
                raise ValueError("top-level JSON value is not an object")
            records.append(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append({"line": line_number, "error": type(exc).__name__, "detail": str(exc)})
            records.append({"__invalid__": True})
    return records, lines, errors


def extract_session_id(records: list[dict[str, Any]]) -> str | None:
    for record in records[:10]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("session_id", "id"):
            if payload.get(key):
                return str(payload[key])
    return None


def collect_stats(path: Path, records: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    payload_type_counts: dict[str, int] = {}
    roles: dict[str, int] = {}
    call_ids: list[str] = []
    output_ids: list[str] = []
    image_nodes = 0
    user_image_nodes = 0
    tool_output_bytes = 0
    tool_output_count = 0
    compaction_lines: list[int] = []
    visible_message_lines: list[int] = []
    timestamps: list[str] = []
    for line_number, record in enumerate(records, start=1):
        if record.get("__invalid__"):
            continue
        top_type = str(record.get("type", ""))
        type_counts[top_type] = type_counts.get(top_type, 0) + 1
        kind = payload_type(record)
        if kind:
            payload_type_counts[kind] = payload_type_counts.get(kind, 0) + 1
        role = payload_role(record)
        if role:
            roles[role] = roles.get(role, 0) + 1
        if is_compaction(record):
            compaction_lines.append(line_number)
        if is_visible_message(record):
            visible_message_lines.append(line_number)
            user_image_nodes += count_image_nodes(record.get("payload")) if payload_role(record) == "user" or payload_type(record) == "user_message" else 0
        if kind in TOOL_CALL_TYPES and get_call_id(record):
            call_ids.append(get_call_id(record) or "")
        if kind in TOOL_OUTPUT_TYPES:
            output_id = get_call_id(record)
            if output_id:
                output_ids.append(output_id)
            tool_output_count += 1
            image_nodes += count_image_nodes(record.get("payload", {}).get("output"))
            tool_output_bytes += len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            timestamps.append(timestamp)
    call_set = set(call_ids)
    output_set = set(output_ids)
    return {
        "record_count": len(records),
        "invalid_record_count": len(errors),
        "errors": errors,
        "type_counts": dict(sorted(type_counts.items())),
        "payload_type_counts": dict(sorted(payload_type_counts.items())),
        "roles": dict(sorted(roles.items())),
        "session_id": extract_session_id(records),
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
        "compaction_lines": compaction_lines,
        "visible_message_lines": visible_message_lines,
        "tool_call_count": len(call_ids),
        "tool_output_count": tool_output_count,
        "tool_call_ids": call_ids,
        "tool_output_call_ids": output_ids,
        "pending_call_ids": sorted(call_set - output_set),
        "orphan_output_ids": sorted(output_set - call_set),
        "image_payload_count": image_nodes,
        "user_image_payload_count": user_image_nodes,
        "tool_output_bytes": tool_output_bytes,
    }


def find_locks(path: Path) -> list[str]:
    locks: list[str] = []
    basename = path.name
    session_id = extract_session_id(parse_jsonl(path)[0]) if path.exists() else None
    candidates = [path.with_name(basename + ".lock"), path.with_name(basename + ".lck")]
    for ancestor in [path.parent, *path.parents]:
        if ancestor.name == ".codex":
            if session_id:
                candidates.extend(
                    [
                        ancestor / "thread-writer-locks" / f"{session_id}.lock",
                        ancestor / "thread-writer-locks" / f"{session_id}",
                    ]
                )
            break
    for candidate in candidates:
        if str(candidate) and candidate.exists():
            locks.append(str(candidate.resolve()))
    return sorted(set(locks))


def recent_boundary(records: list[dict[str, Any]], recent_records: int) -> tuple[int, str]:
    compactions = [line for line, record in enumerate(records, start=1) if is_compaction(record)]
    if compactions:
        return compactions[-1], "latest_compaction"
    return max(1, len(records) - recent_records + 1), "recent_tail_fallback"


def validate_cleanup_options(
    recent_records: int,
    max_output_bytes: int,
    prefix_bytes: int,
    suffix_bytes: int,
) -> None:
    if recent_records < 1:
        raise ValueError("recent_records must be at least 1")
    if max_output_bytes < 1024:
        raise ValueError("max_output_bytes must be at least 1024")
    if prefix_bytes < 1 or suffix_bytes < 1:
        raise ValueError("prefix_bytes and suffix_bytes must be at least 1")
    if prefix_bytes + suffix_bytes >= max_output_bytes:
        raise ValueError("prefix_bytes plus suffix_bytes must be less than max_output_bytes")


def transform_lines(
    records: list[dict[str, Any]],
    raw_lines: list[bytes],
    protected_from: int,
    max_output_bytes: int,
    prefix_bytes: int,
    suffix_bytes: int,
) -> dict[str, Any]:
    changed_lines: list[dict[str, Any]] = []
    candidate_lines: list[bytes] = []
    image_payloads_cleared = 0
    truncated_outputs = 0
    bytes_saved = 0
    for line_number, (record, raw_line) in enumerate(zip(records, raw_lines), start=1):
        if record.get("__invalid__") or line_number >= protected_from or is_visible_message(record):
            candidate_lines.append(raw_line)
            continue
        kind = payload_type(record)
        if kind not in TOOL_OUTPUT_TYPES:
            candidate_lines.append(raw_line)
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or "output" not in payload:
            candidate_lines.append(raw_line)
            continue
        scrubbed_output, image_count = scrub_image_nodes(payload["output"])
        truncated_output, did_truncate = truncate_output(
            scrubbed_output, max_output_bytes, prefix_bytes, suffix_bytes
        )
        if image_count == 0 and not did_truncate:
            candidate_lines.append(raw_line)
            continue
        updated = copy.deepcopy(record)
        updated["payload"]["output"] = truncated_output
        content = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        newline = b"\r\n" if raw_line.endswith(b"\r\n") else b"\n" if raw_line.endswith(b"\n") else b""
        changed = content + newline
        candidate_lines.append(changed)
        image_payloads_cleared += image_count
        truncated_outputs += int(did_truncate)
        bytes_saved += max(0, len(raw_line) - len(changed))
        changed_lines.append(
            {
                "line": line_number,
                "payload_type": kind,
                "call_id": get_call_id(record),
                "original_bytes": len(raw_line),
                "candidate_bytes": len(changed),
                "original_sha256": sha256_bytes(raw_line),
                "candidate_sha256": sha256_bytes(changed),
                "image_payloads_cleared": image_count,
                "truncated": did_truncate,
                "reason": "old_tool_output_image_cache" if image_count and not did_truncate else "old_tool_output_size",
            }
        )
    return {
        "candidate_lines": candidate_lines,
        "changed_lines": changed_lines,
        "changed_records": len(changed_lines),
        "image_payloads_cleared": image_payloads_cleared,
        "truncated_outputs": truncated_outputs,
        "bytes_saved": bytes_saved,
    }


def write_candidate(
    source: Path,
    candidate: Path,
    records: list[dict[str, Any]],
    raw_lines: list[bytes],
    protected_from: int,
    max_output_bytes: int,
    prefix_bytes: int,
    suffix_bytes: int,
) -> dict[str, Any]:
    transformed = transform_lines(
        records,
        raw_lines,
        protected_from,
        max_output_bytes,
        prefix_bytes,
        suffix_bytes,
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(transformed["candidate_lines"])
    with candidate.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(candidate.parent)
    return {
        "changed_lines": transformed["changed_lines"],
        "changed_records": transformed["changed_records"],
        "image_payloads_cleared": transformed["image_payloads_cleared"],
        "truncated_outputs": transformed["truncated_outputs"],
        "bytes_saved": transformed["bytes_saved"],
        "candidate_bytes": candidate.stat().st_size,
    }


def inspect_file(path: Path) -> dict[str, Any]:
    path = validate_session_path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    records, _, errors = parse_jsonl(path)
    source = fingerprint(path)
    stats = collect_stats(path, records, errors)
    return {
        "report_version": 1,
        "report_id": uuid.uuid4().hex,
        "created_at": utc_now(),
        "source": source,
        "stats": stats,
        "locks": find_locks(path),
        "safe_to_plan": bool(records) and not errors and not find_locks(path),
        "policy": {
            "scope": "one specified JSONL session file",
            "disk_history_only": True,
            "never_modify_session_index_or_sqlite": True,
            "default_action": "preserve_messages_and_recent_records; scrub_old_tool_output_images; truncate_only_oversized_old_tool_outputs",
        },
    }


def save_inspection(report: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    """Persist the read-only inspection report without touching the session."""
    report_path = report_dir.expanduser().resolve() / f"inspect-{report['report_id']}.json"
    write_json(report_path, report)
    result = dict(report)
    result["report_path"] = str(report_path)
    return result


def build_plan(
    source: Path,
    report_dir: Path,
    *,
    recent_records: int = DEFAULT_RECENT_RECORDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    prefix_bytes: int = DEFAULT_PREFIX_BYTES,
    suffix_bytes: int = DEFAULT_SUFFIX_BYTES,
) -> dict[str, Any]:
    validate_cleanup_options(recent_records, max_output_bytes, prefix_bytes, suffix_bytes)
    source = validate_session_path(source)
    report_dir = report_dir.expanduser().resolve()
    records, raw_lines, errors = parse_jsonl(source)
    if not records:
        errors.append({"line": 0, "error": "empty_session"})
    source_fingerprint = fingerprint(source)
    stats = collect_stats(source, records, errors)
    protected_from, protected_reason = recent_boundary(records, recent_records)
    plan_id = uuid.uuid4().hex
    candidate = report_dir / f"candidate-{plan_id}.jsonl"
    transformation = write_candidate(
        source,
        candidate,
        records,
        raw_lines,
        protected_from,
        max_output_bytes,
        prefix_bytes,
        suffix_bytes,
    )
    candidate_fingerprint = fingerprint(candidate)
    plan = {
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "status": "ready_for_review" if not errors else "blocked",
        "created_at": utc_now(),
        "source": source_fingerprint,
        "candidate": candidate_fingerprint,
        "candidate_path": str(candidate),
        "report_path": str(report_dir / f"plan-{plan_id}.json"),
        "audit_path": str(report_dir / f"audit-{plan_id}.json"),
        "session_id": stats["session_id"],
        "source_stats": stats,
        "locks": find_locks(source),
        "protected_region": {
            "from_line": protected_from,
            "reason": protected_reason,
            "includes_from_line": True,
            "rules": [
                "preserve all records from the latest compaction boundary",
                "preserve all user and assistant visible messages byte-for-byte",
                "preserve all non-tool-output records and tool call IDs",
                "preserve images embedded in user messages",
            ],
        },
        "transformation": {
            "old_tool_output_only": True,
            "max_output_bytes": max_output_bytes,
            "prefix_bytes": prefix_bytes,
            "suffix_bytes": suffix_bytes,
            "changed_lines": transformation["changed_lines"],
        },
        "summary": {
            "original_bytes": source.stat().st_size,
            "candidate_bytes": transformation["candidate_bytes"],
            "bytes_saved": transformation["bytes_saved"],
            "changed_records": transformation["changed_records"],
            "image_payloads_cleared": transformation["image_payloads_cleared"],
            "truncated_outputs": transformation["truncated_outputs"],
        },
        "review_requirements": [
            "independent audit must pass",
            "source SHA-256 must still match at apply time",
            "an explicit confirmation equal to plan_id is required",
            "a byte-level backup must be verified before replacement",
        ],
    }
    if errors or plan["locks"]:
        plan["status"] = "blocked"
        plan["blocking_reasons"] = (["invalid_json"] if errors else []) + (["writer_lock_detected"] if plan["locks"] else [])
    plan["plan_digest"] = self_digest(plan, "plan_digest")
    write_json(Path(plan["report_path"]), plan)
    return plan


def canonical_record(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compare_sequences(source_records: list[dict[str, Any]], candidate_records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    source_calls = [get_call_id(r) for r in source_records if payload_type(r) in TOOL_CALL_TYPES]
    candidate_calls = [get_call_id(r) for r in candidate_records if payload_type(r) in TOOL_CALL_TYPES]
    source_outputs = [get_call_id(r) for r in source_records if payload_type(r) in TOOL_OUTPUT_TYPES]
    candidate_outputs = [get_call_id(r) for r in candidate_records if payload_type(r) in TOOL_OUTPUT_TYPES]
    if source_calls != candidate_calls:
        errors.append("tool call ID sequence changed")
    if source_outputs != candidate_outputs:
        errors.append("tool output call ID sequence changed")
    return errors


def audit_file_snapshot(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "is_file": False,
            "sha256": None,
            "size": None,
            "records": [],
            "lines": [],
            "errors": [],
        }
    if not resolved.is_file():
        return {
            "path": str(resolved),
            "exists": True,
            "is_file": False,
            "sha256": None,
            "size": None,
            "records": [],
            "lines": [],
            "errors": [{"line": 0, "error": "not_a_regular_file"}],
        }
    try:
        records, lines, errors = parse_jsonl(resolved)
        return {
            "path": str(resolved),
            "exists": True,
            "is_file": True,
            "sha256": sha256_file(resolved),
            "size": resolved.stat().st_size,
            "records": records,
            "lines": lines,
            "errors": errors,
        }
    except OSError as exc:
        return {
            "path": str(resolved),
            "exists": True,
            "is_file": True,
            "sha256": None,
            "size": None,
            "records": [],
            "lines": [],
            "errors": [{"line": 0, "error": type(exc).__name__, "detail": str(exc)}],
        }


def audit_snapshot_input(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": snapshot["path"],
        "exists": snapshot["exists"],
        "is_file": snapshot["is_file"],
        "sha256": snapshot["sha256"],
        "size": snapshot["size"],
        "record_count": len(snapshot["records"]),
        "line_sha256": [sha256_bytes(line) for line in snapshot["lines"]],
        "errors": snapshot["errors"],
    }


def make_audit_stage(
    name: str,
    status: str,
    checks: list[str],
    errors: list[str],
    input_data: dict[str, Any],
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "name": name,
        "status": status,
        "checks": checks,
        "errors": errors,
        "input_digest": value_digest(input_data),
    }
    if observations is not None:
        stage["observations"] = observations
    stage["result_digest"] = self_digest(stage, "result_digest")
    return stage


def validate_audit_stages(audit: dict[str, Any]) -> list[str]:
    stages = audit.get("stages")
    if not isinstance(stages, list):
        return ["audit stages are missing"]
    names = [stage.get("name") if isinstance(stage, dict) else None for stage in stages]
    if names != list(AUDIT_STAGE_NAMES):
        return ["audit stages are missing, duplicated, or out of order"]
    errors: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict):
            errors.append("audit stage is not an object")
            continue
        if not isinstance(stage.get("input_digest"), str):
            errors.append(f"{stage['name']} stage input digest is missing")
        if stage.get("status") != "pass":
            errors.append(f"{stage['name']} stage did not pass")
        if stage.get("result_digest") != self_digest(stage, "result_digest"):
            errors.append(f"{stage['name']} stage result digest changed")
    return errors


def audit_matches_current_files(plan: dict[str, Any], audit: dict[str, Any]) -> list[str]:
    source = validate_session_path(Path(plan["source"]["path"]))
    candidate = Path(plan["candidate_path"]).expanduser().resolve()
    expected_stages = build_audit_stages(source, candidate, plan)
    actual_stages = audit.get("stages")
    if not isinstance(actual_stages, list):
        return ["audit stages are missing"]
    if len(actual_stages) != len(expected_stages):
        return ["audit stage count does not match a fresh audit"]
    errors: list[str] = []
    for expected, actual in zip(expected_stages, actual_stages):
        if actual != expected:
            errors.append(f"{expected['name']} stage does not match a fresh audit")
    return errors


def audit_schema_stage(source: Path, candidate: Path, plan: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = audit_file_snapshot(source)
    candidate_snapshot = audit_file_snapshot(candidate)
    errors: list[str] = []
    if not source_snapshot["exists"]:
        errors.append("source file is missing")
    elif not source_snapshot["is_file"]:
        errors.append("source is not a regular file")
    if not candidate_snapshot["exists"]:
        errors.append("candidate file is missing")
    elif not candidate_snapshot["is_file"]:
        errors.append("candidate is not a regular file")
    errors.extend(
        f"source line {item['line']}: {item['error']}" for item in source_snapshot["errors"]
    )
    errors.extend(
        f"candidate line {item['line']}: {item['error']}" for item in candidate_snapshot["errors"]
    )
    if source_snapshot["exists"] and not source_snapshot["records"]:
        errors.append("source is empty")
    if candidate_snapshot["exists"] and not candidate_snapshot["records"]:
        errors.append("candidate is empty")
    input_data = {
        "plan_hashes": {
            "source": plan.get("source", {}).get("sha256"),
            "candidate": plan.get("candidate", {}).get("sha256"),
        },
        "source": audit_snapshot_input(source_snapshot),
        "candidate": audit_snapshot_input(candidate_snapshot),
    }
    return make_audit_stage(
        "schema",
        "pass" if not errors else "fail",
        ["source and candidate are non-empty UTF-8 JSONL objects"],
        errors,
        input_data,
        {
            "source_records": len(source_snapshot["records"]),
            "candidate_records": len(candidate_snapshot["records"]),
        },
    )


def audit_policy_stage(source: Path, candidate: Path, plan: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = audit_file_snapshot(source)
    candidate_snapshot = audit_file_snapshot(candidate)
    source_records = source_snapshot["records"]
    candidate_records = candidate_snapshot["records"]
    source_lines = source_snapshot["lines"]
    candidate_lines = candidate_snapshot["lines"]
    errors: list[str] = []
    changed_lines = {
        item["line"] for item in plan.get("transformation", {}).get("changed_lines", [])
    }
    protected_from = int(plan.get("protected_region", {}).get("from_line", 1))
    for line_number, (source_record, candidate_record, source_line, candidate_line) in enumerate(
        zip(source_records, candidate_records, source_lines, candidate_lines), start=1
    ):
        if line_number not in changed_lines and source_line != candidate_line:
            errors.append(f"unexpected byte change at line {line_number}")
        if line_number >= protected_from and source_line != candidate_line:
            errors.append(f"protected recent record changed at line {line_number}")
        if is_visible_message(source_record) and source_line != candidate_line:
            errors.append(f"protected visible message changed at line {line_number}")
        if is_compaction(source_record) and source_line != candidate_line:
            errors.append(f"compaction record changed at line {line_number}")
        if payload_type(source_record) in TOOL_CALL_TYPES and source_line != candidate_line:
            errors.append(f"tool call record changed at line {line_number}")
        if line_number in changed_lines and payload_type(source_record) not in TOOL_OUTPUT_TYPES:
            errors.append(f"non-tool-output record selected at line {line_number}")
    input_data = {
        "source": audit_snapshot_input(source_snapshot),
        "candidate": audit_snapshot_input(candidate_snapshot),
        "changed_lines": sorted(changed_lines),
        "protected_from": protected_from,
    }
    return make_audit_stage(
        "policy",
        "pass" if not errors else "fail",
        [
            "only declared old tool-output lines changed",
            "protected records and visible messages are byte-for-byte unchanged",
            "recent boundary is intact",
        ],
        errors,
        input_data,
        {"changed_lines_checked": sorted(changed_lines)},
    )


def audit_deterministic_stage(source: Path, candidate: Path, plan: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = audit_file_snapshot(source)
    candidate_snapshot = audit_file_snapshot(candidate)
    source_records = source_snapshot["records"]
    candidate_lines = candidate_snapshot["lines"]
    source_lines = source_snapshot["lines"]
    options = plan.get("transformation", {})
    protected_from = int(plan.get("protected_region", {}).get("from_line", 1))
    errors: list[str] = []
    try:
        max_output_bytes = int(options["max_output_bytes"])
        prefix_bytes = int(options["prefix_bytes"])
        suffix_bytes = int(options["suffix_bytes"])
        validate_cleanup_options(DEFAULT_RECENT_RECORDS, max_output_bytes, prefix_bytes, suffix_bytes)
        expected = transform_lines(
            source_records,
            source_lines,
            protected_from,
            max_output_bytes,
            prefix_bytes,
            suffix_bytes,
        )
        if expected["candidate_lines"] != candidate_lines:
            errors.append("candidate does not match deterministic transform")
        if expected["changed_lines"] != options.get("changed_lines", []):
            errors.append("plan transformation metadata does not match deterministic transform")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid transformation options: {exc}")
    input_data = {
        "source": audit_snapshot_input(source_snapshot),
        "candidate": audit_snapshot_input(candidate_snapshot),
        "protected_from": protected_from,
        "transformation": options,
    }
    return make_audit_stage(
        "deterministic_transform",
        "pass" if not errors else "fail",
        ["candidate is reproduced exactly from source and declared parameters"],
        errors,
        input_data,
        {"candidate_lines": len(candidate_lines)},
    )


def audit_integrity_stage(source: Path, candidate: Path, plan: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = audit_file_snapshot(source)
    candidate_snapshot = audit_file_snapshot(candidate)
    source_records = source_snapshot["records"]
    candidate_records = candidate_snapshot["records"]
    errors: list[str] = []
    if source_snapshot["sha256"] != plan.get("source", {}).get("sha256"):
        errors.append("source SHA-256 changed since plan generation")
    if candidate_snapshot["sha256"] != plan.get("candidate", {}).get("sha256"):
        errors.append("candidate SHA-256 changed since plan generation")
    if len(source_records) != len(candidate_records):
        errors.append("record count changed")
    errors.extend(compare_sequences(source_records, candidate_records))
    source_stats = collect_stats(source, source_records, [])
    candidate_stats = collect_stats(candidate, candidate_records, [])
    if source_stats["session_id"] != candidate_stats["session_id"]:
        errors.append("session ID changed")
    if source_stats["visible_message_lines"] != candidate_stats["visible_message_lines"]:
        errors.append("visible message line sequence changed")
    protected_from = int(plan.get("protected_region", {}).get("from_line", 1))
    old_candidate_images = 0
    for line_number, record in enumerate(candidate_records, start=1):
        payload = record.get("payload")
        output = payload.get("output") if isinstance(payload, dict) else None
        if line_number < protected_from and payload_type(record) in TOOL_OUTPUT_TYPES:
            old_candidate_images += count_image_nodes(output)
    if old_candidate_images:
        errors.append("old tool output still contains embedded image payloads")
    input_data = {
        "source": audit_snapshot_input(source_snapshot),
        "candidate": audit_snapshot_input(candidate_snapshot),
        "protected_from": protected_from,
        "expected_source_sha256": plan.get("source", {}).get("sha256"),
        "expected_candidate_sha256": plan.get("candidate", {}).get("sha256"),
    }
    return make_audit_stage(
        "integrity",
        "pass" if not errors else "fail",
        [
            "source and candidate hashes are unchanged",
            "record count, session ID, and tool ID sequences are stable",
            "old tool-output image nodes are absent",
        ],
        errors,
        input_data,
        {
            "source_records": len(source_records),
            "candidate_records": len(candidate_records),
            "old_candidate_images": old_candidate_images,
        },
    )


def build_audit_stages(source: Path, candidate: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        audit_schema_stage(source, candidate, plan),
        audit_policy_stage(source, candidate, plan),
        audit_deterministic_stage(source, candidate, plan),
        audit_integrity_stage(source, candidate, plan),
    ]


def audit_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    plan = read_json(plan_path)
    source = validate_session_path(Path(plan["source"]["path"]))
    candidate = Path(plan["candidate_path"]).expanduser().resolve()
    plan_errors: list[str] = []
    if plan.get("plan_digest") != self_digest(plan, "plan_digest"):
        plan_errors.append("plan digest changed")
    if plan.get("status") == "blocked":
        plan_errors.append("plan is blocked")
    stage_results = build_audit_stages(source, candidate, plan)
    errors: list[str] = []
    for error in [*plan_errors, *(error for stage in stage_results for error in stage["errors"])]:
        if error not in errors:
            errors.append(error)
    status = "pass" if not errors and all(stage["status"] == "pass" for stage in stage_results) else "fail"
    audit = {
        "audit_version": 2,
        "audit_id": uuid.uuid4().hex,
        "created_at": utc_now(),
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path),
        "source_sha256": plan["source"]["sha256"],
        "candidate_sha256": plan["candidate"]["sha256"],
        "plan_digest": plan.get("plan_digest"),
        "status": status,
        "stages": stage_results,
        "checks": {
            "source_unchanged": not any("source SHA-256" in error for error in errors),
            "candidate_unchanged": not any("candidate SHA-256" in error for error in errors),
            "jsonl_parseable": not any("line" in error and ("source" in error or "candidate" in error) for error in errors),
            "protected_records_unchanged": not any("protected" in error for error in errors),
            "tool_sequences_unchanged": not any("sequence changed" in error for error in errors),
            "old_images_removed": not any("embedded image" in error for error in errors),
        },
        "errors": errors,
    }
    audit["audit_digest"] = self_digest(audit, "audit_digest")
    write_json(Path(plan["audit_path"]), audit)
    return audit


def ensure_inside(root: Path, child: Path) -> None:
    root_resolved = root.expanduser().resolve()
    child_resolved = child.expanduser().resolve()
    try:
        child_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes managed root: {child_resolved}") from exc


def default_backup_root() -> Path:
    return Path.home() / ".codex" / "session-cleanup-backups"


def validate_backup_session_id(session_id: str) -> str:
    if (
        not isinstance(session_id, str)
        or not session_id
        or session_id in {".", ".."}
        or "/" in session_id
        or "\\" in session_id
        or ":" in session_id
    ):
        raise ValueError("session_id must be a single safe path segment")
    return session_id


def validate_preview_id(preview_id: str) -> str:
    if (
        not isinstance(preview_id, str)
        or len(preview_id) != 32
        or any(character not in "0123456789abcdef" for character in preview_id)
    ):
        raise ValueError("preview_id must be a generated 32-character hexadecimal ID")
    return preview_id


def validate_session_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() != ".jsonl":
        raise ValueError(f"target must be a .jsonl session file: {resolved}")
    if resolved.name in RESERVED_NAMES or resolved.name.endswith((".lock", ".lck", ".sqlite", ".sqlite-wal", ".sqlite-shm")):
        raise ValueError(f"target is outside the session JSONL scope: {resolved}")
    return resolved


def apply_plan(plan_path: Path, confirmation: str, backup_root: Path | None = None) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    plan = read_json(plan_path)
    if plan.get("plan_digest") != self_digest(plan, "plan_digest"):
        raise ValueError("plan digest changed after audit; regenerate the plan")
    if confirmation != plan.get("plan_id"):
        raise ValueError("confirmation must exactly equal plan_id")
    audit_path = Path(plan["audit_path"])
    if not audit_path.exists():
        raise ValueError("independent audit is required before apply")
    audit = read_json(audit_path)
    if audit.get("audit_digest") != self_digest(audit, "audit_digest"):
        raise ValueError("audit digest changed after review")
    stage_errors = validate_audit_stages(audit)
    fresh_audit_errors = audit_matches_current_files(plan, audit)
    if audit.get("status") != "pass" or audit.get("plan_id") != plan.get("plan_id"):
        raise ValueError("independent audit did not pass for this plan")
    if stage_errors:
        raise ValueError("audit stage validation failed: " + "; ".join(stage_errors))
    if fresh_audit_errors:
        raise ValueError("audit no longer matches a fresh audit: " + "; ".join(fresh_audit_errors))
    if audit.get("plan_digest") != plan.get("plan_digest"):
        raise ValueError("audit is not bound to the current plan")
    if audit.get("plan_path") != str(plan_path):
        raise ValueError("audit is bound to a different plan path")
    if audit.get("source_sha256") != plan["source"]["sha256"] or audit.get("candidate_sha256") != plan["candidate"]["sha256"]:
        raise ValueError("audit hashes do not match the current plan")
    source = validate_session_path(Path(plan["source"]["path"]))
    candidate = Path(plan["candidate_path"])
    if fingerprint(source)["sha256"] != plan["source"]["sha256"]:
        raise ValueError("source changed after review; regenerate the plan")
    if fingerprint(candidate)["sha256"] != plan["candidate"]["sha256"]:
        raise ValueError("candidate changed after review; regenerate the plan")
    locks = find_locks(source)
    if locks:
        raise ValueError("writer lock detected: " + ", ".join(locks))
    backup_root = (backup_root or default_backup_root()).expanduser().resolve()
    session_id = plan.get("session_id") or source.stem
    validate_backup_session_id(str(session_id))
    backup_id = f"{utc_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    backup_dir = backup_root / str(session_id) / backup_id
    ensure_inside(backup_root, backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=False)
    original_backup = backup_dir / "original.jsonl"
    manifest: dict[str, Any] = {
        "backup_version": 1,
        "backup_id": backup_id,
        "session_id": session_id,
        "status": "preparing",
        "created_at": utc_now(),
        "source_path": str(source),
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path),
        "audit_path": str(audit_path),
    }
    manifest_path = backup_dir / "manifest.json"
    source_mode = source.stat().st_mode
    temp_path: Path | None = None
    replaced = False
    try:
        write_manifest(manifest_path, manifest)
        copy_file_fsync(source, original_backup)
        original_hash = sha256_file(original_backup)
        if original_hash != plan["source"]["sha256"]:
            raise ValueError("backup hash does not match reviewed source")
        manifest.update(
            {
                "status": "prepared",
                "original_sha256": original_hash,
                "original_bytes": original_backup.stat().st_size,
            }
        )
        write_manifest(manifest_path, manifest)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=source.parent, prefix=f".{source.name}.cleanup-", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            with candidate.open("rb") as candidate_handle:
                shutil.copyfileobj(candidate_handle, handle)
        os.chmod(temp_path, source_mode)
        if sha256_file(source) != plan["source"]["sha256"]:
            raise ValueError("source changed during backup or candidate staging")
        os.replace(temp_path, source)
        temp_path = None
        replaced = True
        if sha256_file(source) != plan["candidate"]["sha256"]:
            raise ValueError("post-write hash does not match candidate")
        manifest["status"] = "success"
        manifest["final_sha256"] = sha256_file(source)
        write_manifest(manifest_path, manifest)
    except Exception as error:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        restore_error: Exception | None = None
        if replaced and original_backup.exists() and sha256_file(original_backup) == manifest.get("original_sha256"):
            try:
                copy_file_fsync(original_backup, source)
                os.chmod(source, source_mode)
                if sha256_file(source) != manifest["original_sha256"]:
                    raise ValueError("rollback hash does not match original backup")
            except Exception as rollback_error:  # pragma: no cover - disk/permission dependent
                restore_error = rollback_error
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        if restore_error:
            manifest["rollback_error"] = str(restore_error)
        try:
            write_manifest(manifest_path, manifest)
        except OSError:
            pass
        if restore_error:
            raise RuntimeError(f"apply failed and rollback failed: {error}; {restore_error}") from error
        raise
    return {
        "status": "success",
        "backup_id": backup_id,
        "backup_path": str(backup_dir),
        "source_path": str(source),
        "source_sha256": sha256_file(source),
    }


def list_backups(backup_root: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    backup_root = backup_root.expanduser().resolve()
    if session_id is not None:
        validate_backup_session_id(session_id)
    if not backup_root.exists():
        return []
    roots = (
        [backup_root / session_id]
        if session_id
        else [path for path in backup_root.iterdir() if path.is_dir() and path.name not in BACKUP_INTERNAL_DIRS]
    )
    result: list[dict[str, Any]] = []
    for session_root in roots:
        if not session_root.is_dir() or session_root.name in BACKUP_INTERNAL_DIRS:
            continue
        for batch in session_root.iterdir():
            manifest_path = batch / "manifest.json"
            if not batch.is_dir():
                continue
            if not manifest_path.is_file():
                result.append(
                    {
                        "path": str(batch.resolve()),
                        "session_id": session_root.name,
                        "status": "unknown",
                        "integrity": "unknown",
                        "reason": "manifest.json is missing",
                    }
                )
                continue
            try:
                manifest = read_json(manifest_path)
                manifest["path"] = str(batch.resolve())
                if manifest.get("status") == "success":
                    is_valid, reason = backup_integrity(manifest, backup_root, session_root.name)
                    manifest["integrity"] = "valid" if is_valid else "invalid"
                    if not is_valid:
                        manifest["integrity_error"] = reason
                else:
                    manifest["integrity"] = "not_checked"
                result.append(manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                result.append(
                    {
                        "path": str(batch.resolve()),
                        "session_id": session_root.name,
                        "status": "unknown",
                        "integrity": "unknown",
                        "reason": "manifest.json is unreadable",
                    }
                )
    return sorted(result, key=lambda item: str(item.get("created_at", "")), reverse=True)


def backup_integrity(entry: dict[str, Any], backup_root: Path, session_id: str) -> tuple[bool, str]:
    path = Path(entry.get("path", "")).resolve()
    try:
        ensure_inside(backup_root, path)
    except ValueError as error:
        return False, str(error)
    if entry.get("session_id") != session_id or entry.get("status") != "success":
        return False, "not a successful backup for this session"
    if not path.is_dir() or not (path / "manifest.json").is_file() or not (path / "original.jsonl").is_file():
        return False, "backup files are incomplete"
    try:
        manifest = read_json(path / "manifest.json")
        if manifest.get("backup_id") != entry.get("backup_id"):
            return False, "manifest identity changed"
        if manifest.get("manifest_digest") != self_digest(manifest, "manifest_digest"):
            return False, "manifest digest mismatch"
        source_path = manifest.get("source_path")
        if not isinstance(source_path, str):
            return False, "manifest source path is missing"
        validate_session_path(Path(source_path))
        if sha256_file(path / "original.jsonl") != manifest.get("original_sha256"):
            return False, "original backup hash mismatch"
        records, _, errors = parse_jsonl(path / "original.jsonl")
        if not records or errors:
            return False, "original backup is not valid JSONL"
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return False, str(error)
    return True, "ok"


def prune_snapshot(entries: list[dict[str, Any]], backup_root: Path, session_id: str, keep: int) -> dict[str, Any]:
    valid: list[dict[str, Any]] = []
    preserved: list[str] = []
    invalid_reasons: dict[str, str] = {}
    for entry in entries:
        if entry.get("status") == "success":
            is_valid, reason = backup_integrity(entry, backup_root, session_id)
            if is_valid:
                valid.append(entry)
            else:
                path = str(Path(entry.get("path", "")).resolve())
                preserved.append(path)
                invalid_reasons[path] = reason
        else:
            if entry.get("path"):
                preserved.append(str(Path(entry["path"]).resolve()))
    candidates = valid[keep:]
    candidate_paths = [str(Path(entry["path"]).resolve()) for entry in candidates]
    snapshot = []
    for entry in entries:
        if not entry.get("path"):
            continue
        entry_path = Path(entry["path"]).resolve()
        manifest_path = entry_path / "manifest.json"
        snapshot.append(
            {
                "path": str(entry_path),
                "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
                "original_sha256": entry.get("original_sha256"),
            }
        )
    return {
        "session_id": session_id,
        "keep_successful": keep,
        "candidate_paths": candidate_paths,
        "preserved_paths": sorted(set(preserved)),
        "invalid_reasons": invalid_reasons,
        "snapshot": snapshot,
    }


def prune_backups(backup_root: Path, session_id: str, *, keep: int = 2, confirm: str | bool | None = None) -> dict[str, Any]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    validate_backup_session_id(session_id)
    backup_root = backup_root.expanduser().resolve()
    entries = list_backups(backup_root, session_id)
    if confirm is True:
        raise ValueError("prune confirmation must be the preview_id returned by a prior preview")
    if isinstance(confirm, str):
        validate_preview_id(confirm)
        preview_path = backup_root / ".prune-previews" / f"{confirm}.json"
        if not preview_path.is_file():
            raise ValueError("unknown or expired prune preview_id")
        preview = read_json(preview_path)
        if preview.get("preview_id") != confirm:
            raise ValueError("prune preview identity mismatch")
        if preview.get("preview_digest") != self_digest(preview, "preview_digest"):
            raise ValueError("prune preview was changed after creation")
        snapshot = prune_snapshot(entries, backup_root, session_id, keep)
        if snapshot["snapshot"] != preview.get("snapshot") or snapshot["candidate_paths"] != preview.get("candidate_paths"):
            raise ValueError("backup set changed after preview; create a new preview")
        paths = [Path(path).resolve() for path in preview["candidate_paths"]]
        for path in paths:
            ensure_inside(backup_root, path)
            if not path.is_dir():
                raise ValueError(f"backup candidate is no longer a directory: {path}")
            current_entry = next((entry for entry in entries if Path(entry.get("path", "")).resolve() == path), None)
            if current_entry is None:
                raise ValueError(f"backup candidate disappeared: {path}")
            is_valid, reason = backup_integrity(current_entry, backup_root, session_id)
            if not is_valid:
                raise ValueError(f"backup candidate failed final integrity check: {path}: {reason}")
        quarantine_root = backup_root / ".prune-quarantine" / confirm
        ensure_inside(backup_root, quarantine_root)
        quarantine_root.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            for path in paths:
                quarantine_path = quarantine_root / path.name
                os.replace(path, quarantine_path)
                moved.append((path, quarantine_path))
            fsync_directory(quarantine_root)
        except Exception as error:
            rollback_errors: list[str] = []
            for original_path, quarantine_path in reversed(moved):
                try:
                    os.replace(quarantine_path, original_path)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if not rollback_errors:
                try:
                    quarantine_root.rmdir()
                except OSError:
                    pass
            detail = f"prune move failed: {error}"
            if rollback_errors:
                detail += "; quarantine rollback failed: " + "; ".join(rollback_errors)
            raise RuntimeError(detail) from error
        try:
            for path in quarantine_root.iterdir():
                shutil.rmtree(path)
            quarantine_root.rmdir()
            fsync_directory(quarantine_root.parent)
        except Exception as error:
            raise RuntimeError(
                f"prune deletion failed; candidates remain in quarantine: {quarantine_root}: {error}"
            ) from error
        return {
            "status": "success",
            "preview_id": confirm,
            "session_id": session_id,
            "deleted_count": len(paths),
            "deleted_paths": [str(path) for path in paths],
        }
    snapshot = prune_snapshot(entries, backup_root, session_id, keep)
    preview_id = uuid.uuid4().hex
    preview = {
        "preview_version": 1,
        "preview_id": preview_id,
        "created_at": utc_now(),
        **snapshot,
    }
    preview["preview_digest"] = self_digest(preview, "preview_digest")
    preview_path = backup_root / ".prune-previews" / f"{preview_id}.json"
    write_json(preview_path, preview)
    return {
        "status": "preview",
        "preview_id": preview_id,
        "session_id": session_id,
        "keep_successful": keep,
        "delete_count": len(snapshot["candidate_paths"]),
        "delete_paths": snapshot["candidate_paths"],
        "preserved_paths": snapshot["preserved_paths"],
        "invalid_reasons": snapshot["invalid_reasons"],
    }


def restore_backup(backup_dir: Path, confirmation: str, target: Path | None = None) -> dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    manifest = read_json(backup_dir / "manifest.json")
    if manifest.get("manifest_digest") != self_digest(manifest, "manifest_digest"):
        raise ValueError("manifest digest mismatch")
    if confirmation != manifest.get("backup_id"):
        raise ValueError("confirmation must exactly equal backup_id")
    if manifest.get("status") != "success":
        raise ValueError("only successful backups can be restored")
    original = backup_dir / "original.jsonl"
    if sha256_file(original) != manifest.get("original_sha256"):
        raise ValueError("backup content hash mismatch")
    backup_records, _, backup_errors = parse_jsonl(original)
    if not backup_records or backup_errors:
        raise ValueError("backup content is not valid JSONL")
    destination = Path(manifest["source_path"]).expanduser().resolve()
    if target is not None and target.expanduser().resolve() != destination:
        raise ValueError("restore target must exactly match the original source path")
    validate_session_path(destination)
    if find_locks(destination):
        raise ValueError("writer lock detected for restore target")
    destination_existed = destination.exists()
    destination_mode = destination.stat().st_mode if destination_existed else None
    rollback_path: Path | None = None
    rollback_sha256: str | None = None
    temp_path: Path | None = None
    replaced = False
    try:
        if destination_existed:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.restore-rollback-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                rollback_path = Path(handle.name)
                with destination.open("rb") as destination_handle:
                    shutil.copyfileobj(destination_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            rollback_sha256 = sha256_file(rollback_path)
            if destination_mode is not None:
                os.chmod(rollback_path, destination_mode)

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.restore-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            with original.open("rb") as original_handle:
                shutil.copyfileobj(original_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if destination_mode is not None:
            os.chmod(temp_path, destination_mode)

        os.replace(temp_path, destination)
        temp_path = None
        replaced = True
        fsync_directory(destination.parent)

        if sha256_file(destination) != manifest["original_sha256"]:
            raise ValueError("restored file hash mismatch")
        restored_records, _, restored_errors = parse_jsonl(destination)
        if not restored_records or restored_errors:
            raise ValueError("restored file is not valid JSONL")
        return {"status": "success", "target": str(destination), "sha256": manifest["original_sha256"]}
    except Exception as error:
        rollback_error: Exception | None = None
        if replaced:
            if rollback_path and rollback_path.exists():
                try:
                    os.replace(rollback_path, destination)
                    rollback_path = None
                    if destination_mode is not None:
                        os.chmod(destination, destination_mode)
                    fsync_directory(destination.parent)
                    if rollback_sha256 is None or sha256_file(destination) != rollback_sha256:
                        raise ValueError("rollback hash check failed")
                except Exception as restore_error:
                    rollback_error = restore_error
            elif not destination_existed and destination.exists():
                try:
                    destination.unlink()
                    fsync_directory(destination.parent)
                except Exception as restore_error:
                    rollback_error = restore_error
        if rollback_error:
            raise RuntimeError(f"restore validation failed and rollback failed: {error}; {rollback_error}") from error
        raise
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        if rollback_path and rollback_path.exists():
            rollback_path.unlink()


def resolve_target(target: str, codex_home: Path | None = None) -> Path:
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    root = (codex_home or (Path.home() / ".codex")).expanduser().resolve()
    index = root / "session_index.jsonl"
    matches: set[str] = set()
    if index.is_file():
        for raw_line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("id", ""))
            name = str(item.get("thread_name", ""))
            if target == session_id or (target and target.casefold() in name.casefold()):
                matches.add(session_id)
    if not matches:
        matches.add(target)
    files: list[Path] = []
    for directory in (root / "sessions", root / "archived_sessions"):
        if not directory.exists():
            continue
        for session_id in matches:
            files.extend(path for path in directory.rglob(f"*{session_id}*.jsonl") if path.is_file())
    files = sorted(set(path.resolve() for path in files))
    if len(files) != 1:
        if not files:
            raise FileNotFoundError(f"no unique session matches target: {target}")
        raise ValueError("target matches multiple session files: " + ", ".join(str(path) for path in files))
    return files[0]


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "plan"):
        sub = subparsers.add_parser(command)
        sub.add_argument("target")
        sub.add_argument("--codex-home", type=Path)
        sub.add_argument("--report-dir", type=Path, default=Path.cwd() / "session-cleanup-reports")
        if command == "plan":
            sub.add_argument("--recent-records", type=int, default=DEFAULT_RECENT_RECORDS)
            sub.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
            sub.add_argument("--prefix-bytes", type=int, default=DEFAULT_PREFIX_BYTES)
            sub.add_argument("--suffix-bytes", type=int, default=DEFAULT_SUFFIX_BYTES)
    audit = subparsers.add_parser("audit")
    audit.add_argument("plan", type=Path)
    apply = subparsers.add_parser("apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--confirm", required=True)
    apply.add_argument("--backup-root", type=Path)
    backups = subparsers.add_parser("backups")
    backups_sub = backups.add_subparsers(dest="backups_command", required=True)
    list_parser = backups_sub.add_parser("list")
    list_parser.add_argument("--backup-root", type=Path, default=default_backup_root())
    list_parser.add_argument("--session-id")
    prune = backups_sub.add_parser("prune")
    prune.add_argument("--backup-root", type=Path, default=default_backup_root())
    prune.add_argument("--session-id", required=True)
    prune.add_argument("--keep", type=int, default=2)
    prune.add_argument("--confirm", help="preview_id returned by a prior prune preview")
    restore = subparsers.add_parser("restore")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--confirm", required=True)
    restore.add_argument("--target", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            print_json(save_inspection(inspect_file(resolve_target(args.target, args.codex_home)), args.report_dir))
        elif args.command == "plan":
            plan = build_plan(
                resolve_target(args.target, args.codex_home),
                args.report_dir,
                recent_records=args.recent_records,
                max_output_bytes=args.max_output_bytes,
                prefix_bytes=args.prefix_bytes,
                suffix_bytes=args.suffix_bytes,
            )
            print_json(plan)
            return 0 if plan["status"] == "ready_for_review" else 2
        elif args.command == "audit":
            audit = audit_plan(args.plan)
            print_json(audit)
            return 0 if audit["status"] == "pass" else 1
        elif args.command == "apply":
            print_json(apply_plan(args.plan, args.confirm, args.backup_root))
        elif args.command == "backups":
            if args.backups_command == "list":
                print_json(list_backups(args.backup_root, args.session_id))
            else:
                print_json(prune_backups(args.backup_root, args.session_id, keep=args.keep, confirm=args.confirm))
        elif args.command == "restore":
            print_json(restore_backup(args.backup_dir, args.confirm, args.target))
        return 0
    except (FileNotFoundError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
