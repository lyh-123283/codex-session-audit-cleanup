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
DEFAULT_RECENT_COMPACTIONS = 2
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_PREFIX_BYTES = 8 * 1024
DEFAULT_SUFFIX_BYTES = 4 * 1024
PLAN_VERSION = 3
BACKUP_VERSION = 1
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
        "max_output_bytes": DEFAULT_MAX_OUTPUT_BYTES,
        "prefix_bytes": DEFAULT_PREFIX_BYTES,
        "suffix_bytes": DEFAULT_SUFFIX_BYTES,
    },
    "space": {
        "profile": "space",
        "scrub_images": True,
        "max_output_bytes": 16 * 1024,
        "prefix_bytes": 2 * 1024,
        "suffix_bytes": 1 * 1024,
    },
}
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


def logical_compaction_boundaries(records: list[dict[str, Any]]) -> list[int]:
    """Return one start line per logical compaction event.

    Codex normally writes a ``compacted`` record followed by a
    ``context_compacted`` event for one compaction. The former starts the
    logical boundary; the first later context marker consumes that pair. A
    context marker without a pending compacted record starts its own boundary.
    """
    boundaries: list[int] = []
    pending_compacted = False
    for line_number, record in enumerate(records, start=1):
        if record.get("type") == "compacted":
            boundaries.append(line_number)
            pending_compacted = True
        elif payload_type(record) == "context_compacted":
            if pending_compacted:
                pending_compacted = False
            else:
                boundaries.append(line_number)
    return boundaries


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


def decode_utf8_prefix(data: bytes, limit: int) -> str:
    fragment = data[:limit]
    while fragment:
        try:
            return fragment.decode("utf-8")
        except UnicodeDecodeError as error:
            fragment = fragment[:error.start]
    return ""


def decode_utf8_suffix(data: bytes, limit: int) -> str:
    fragment = data[-limit:] if limit else b""
    while fragment:
        try:
            return fragment.decode("utf-8")
        except UnicodeDecodeError as error:
            fragment = fragment[error.end:]
    return ""


def truncate_output(value: Any, max_bytes: int, prefix_bytes: int, suffix_bytes: int) -> tuple[Any, bool]:
    encoded = compact_json_bytes(value)
    if len(encoded) <= max_bytes:
        return value, False
    prefix = decode_utf8_prefix(encoded, prefix_bytes) if prefix_bytes else ""
    suffix = decode_utf8_suffix(encoded, suffix_bytes) if suffix_bytes else ""
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
    logical_compaction_lines = logical_compaction_boundaries(records)
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
        "logical_compaction_lines": logical_compaction_lines,
        "logical_compaction_count": len(logical_compaction_lines),
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


def recent_boundary(
    records: list[dict[str, Any]],
    recent_records: int,
    recent_compactions: int = DEFAULT_RECENT_COMPACTIONS,
) -> tuple[int, str]:
    if recent_compactions < 1:
        raise ValueError("recent_compactions must be at least 1")
    compactions = logical_compaction_boundaries(records)
    if compactions:
        start = max(0, len(compactions) - recent_compactions)
        if recent_compactions == 1:
            reason = "latest_compaction"
        elif len(compactions) < recent_compactions:
            reason = "available_logical_compactions"
        else:
            reason = "latest_logical_compactions"
        return compactions[start], reason
    return max(1, len(records) - recent_records + 1), "recent_tail_fallback"


def boundary_metadata(
    records: list[dict[str, Any]],
    recent_records: int,
    recent_compactions: int,
) -> dict[str, Any]:
    compaction_boundaries = logical_compaction_boundaries(records)
    protected_from, protected_reason = recent_boundary(
        records, recent_records, recent_compactions
    )
    selected_compaction_boundaries = compaction_boundaries[
        max(0, len(compaction_boundaries) - recent_compactions) :
    ]
    return {
        "from_line": protected_from,
        "reason": protected_reason,
        "includes_from_line": True,
        "logical_compactions": len(selected_compaction_boundaries),
        "requested_logical_compactions": recent_compactions,
        "available_logical_compactions": len(compaction_boundaries),
        "selected_boundary_lines": selected_compaction_boundaries,
        "fallback_recent_records": recent_records,
    }


def plan_boundary_metadata(
    records: list[dict[str, Any]], plan: dict[str, Any]
) -> tuple[int, list[str], dict[str, Any]]:
    region = plan.get("protected_region")
    if not isinstance(region, dict):
        return 1, ["protected region metadata is missing"], {}

    if plan.get("plan_version") != PLAN_VERSION:
        return 1, ["unsupported plan version; regenerate the plan"], {}
    if "requested_logical_compactions" not in region:
        return 1, ["protected boundary metadata is missing; regenerate the plan"], {}

    errors: list[str] = []
    try:
        recent_compactions = int(region["requested_logical_compactions"])
        recent_records = int(region.get("fallback_recent_records", DEFAULT_RECENT_RECORDS))
        validate_cleanup_options(
            recent_records,
            DEFAULT_MAX_OUTPUT_BYTES,
            DEFAULT_PREFIX_BYTES,
            DEFAULT_SUFFIX_BYTES,
            recent_compactions,
        )
        expected = boundary_metadata(records, recent_records, recent_compactions)
    except (KeyError, TypeError, ValueError) as exc:
        return 1, [f"invalid protected region metadata: {exc}"], {}

    for field in (
        "from_line",
        "reason",
        "includes_from_line",
        "logical_compactions",
        "requested_logical_compactions",
        "available_logical_compactions",
        "selected_boundary_lines",
        "fallback_recent_records",
    ):
        if region.get(field) != expected[field]:
            errors.append(f"protected boundary metadata mismatch: {field}")
    return expected["from_line"], errors, expected


def validate_cleanup_options(
    recent_records: int,
    max_output_bytes: int,
    prefix_bytes: int,
    suffix_bytes: int,
    recent_compactions: int = DEFAULT_RECENT_COMPACTIONS,
) -> None:
    if recent_records < 1:
        raise ValueError("recent_records must be at least 1")
    if recent_compactions < 1:
        raise ValueError("recent_compactions must be at least 1")
    if max_output_bytes < 1024:
        raise ValueError("max_output_bytes must be at least 1024")
    if prefix_bytes < 1 or suffix_bytes < 1:
        raise ValueError("prefix_bytes and suffix_bytes must be at least 1")
    if prefix_bytes + suffix_bytes >= max_output_bytes:
        raise ValueError("prefix_bytes plus suffix_bytes must be less than max_output_bytes")


def resolve_profile_policy(
    profile: str = "balanced",
    max_output_bytes: int | None = None,
    prefix_bytes: int | None = None,
    suffix_bytes: int | None = None,
) -> dict[str, Any]:
    if profile in PROFILE_POLICIES:
        if any(value is not None for value in (max_output_bytes, prefix_bytes, suffix_bytes)):
            raise ValueError("manual output thresholds require profile=custom")
        return dict(PROFILE_POLICIES[profile])
    if profile != "custom":
        raise ValueError("invalid profile: expected cache, balanced, space, or custom")
    max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
    prefix_bytes = DEFAULT_PREFIX_BYTES if prefix_bytes is None else prefix_bytes
    suffix_bytes = DEFAULT_SUFFIX_BYTES if suffix_bytes is None else suffix_bytes
    validate_cleanup_options(
        DEFAULT_RECENT_RECORDS,
        max_output_bytes,
        prefix_bytes,
        suffix_bytes,
    )
    return {
        "profile": "custom",
        "scrub_images": True,
        "max_output_bytes": max_output_bytes,
        "prefix_bytes": prefix_bytes,
        "suffix_bytes": suffix_bytes,
    }


def validate_plan_policy(plan: dict[str, Any]) -> list[str]:
    policy = plan.get("policy")
    if not isinstance(policy, dict):
        return ["policy metadata is missing; regenerate the plan"]
    transformation = plan.get("transformation")
    if not isinstance(transformation, dict):
        return ["transformation metadata is missing; regenerate the plan"]
    transformation_policy = transformation.get("policy")
    if not isinstance(transformation_policy, dict):
        return ["transformation policy metadata is missing; regenerate the plan"]
    errors: list[str] = []
    if policy != transformation_policy:
        errors.append("policy metadata differs between plan sections")
    profile = policy.get("profile")
    if profile in PROFILE_POLICIES:
        if policy != PROFILE_POLICIES[profile]:
            errors.append("policy metadata does not match the named profile")
        return errors
    if profile != "custom":
        errors.append("invalid policy profile; regenerate the plan")
        return errors
    required = {"profile", "scrub_images", "max_output_bytes", "prefix_bytes", "suffix_bytes"}
    missing = sorted(required - set(policy))
    if missing:
        errors.append("custom policy metadata is incomplete: " + ", ".join(missing))
        return errors
    if policy.get("scrub_images") is not True:
        errors.append("custom policy must preserve image-cache scrubbing")
    try:
        validate_cleanup_options(
            DEFAULT_RECENT_RECORDS,
            int(policy["max_output_bytes"]),
            int(policy["prefix_bytes"]),
            int(policy["suffix_bytes"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid custom policy metadata: {exc}")
    return errors


def resolve_plan_profile(
    profile: str = "balanced",
    *,
    target_bytes: int | None = None,
    max_output_bytes: int | None = None,
    prefix_bytes: int | None = None,
    suffix_bytes: int | None = None,
) -> dict[str, Any]:
    if profile == "target":
        if target_bytes is None or isinstance(target_bytes, bool) or target_bytes < 1:
            raise ValueError("target profile requires target_bytes of at least 1")
        if any(value is not None for value in (max_output_bytes, prefix_bytes, suffix_bytes)):
            raise ValueError("target profile cannot be combined with manual output thresholds")
        return {"profile": "target", "target_bytes": target_bytes}
    if target_bytes is not None:
        raise ValueError("target_bytes requires profile=target")
    return resolve_profile_policy(
        profile,
        max_output_bytes,
        prefix_bytes,
        suffix_bytes,
    )


def interpolate_preview_bytes(max_output_bytes: int) -> tuple[int, int]:
    minimum = 16 * 1024
    maximum = 64 * 1024
    if max_output_bytes < minimum or max_output_bytes > maximum:
        raise ValueError("target interpolation threshold must be between space and balanced")
    span = maximum - minimum
    prefix_bytes = 2 * 1024 + ((max_output_bytes - minimum) * (8 * 1024 - 2 * 1024) // span)
    suffix_bytes = 1 * 1024 + ((max_output_bytes - minimum) * (4 * 1024 - 1 * 1024) // span)
    if prefix_bytes + suffix_bytes >= max_output_bytes:
        suffix_bytes = max(1, max_output_bytes - prefix_bytes - 1)
    return prefix_bytes, suffix_bytes


def select_target_policy(
    records: list[dict[str, Any]],
    raw_lines: list[bytes],
    protected_from: int,
    target_bytes: int,
) -> dict[str, Any]:
    if not isinstance(target_bytes, int) or isinstance(target_bytes, bool) or target_bytes < 1:
        raise ValueError("target_bytes must be at least 1")

    def render(policy: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        transformed = transform_lines(records, raw_lines, protected_from, policy=policy)
        return len(b"".join(transformed["candidate_lines"])), transformed

    source_bytes = sum(len(line) for line in raw_lines)
    balanced_policy = resolve_profile_policy("balanced")
    space_policy = resolve_profile_policy("space")
    balanced_bytes, balanced_transform = render(balanced_policy)
    space_bytes, space_transform = render(space_policy)
    base_target = {
        "target_bytes": target_bytes,
        "lower_profile": "space",
        "upper_profile": "balanced",
        "balanced_candidate_bytes": balanced_bytes,
        "space_candidate_bytes": space_bytes,
        "source_bytes": source_bytes,
        "remaining_protected_bytes": 0,
    }
    if source_bytes <= target_bytes:
        return {
            "status": "no_change",
            "policy": balanced_policy,
            "transformation": balanced_transform,
            "target": {
                **base_target,
                "selection_method": "source_already_within_target",
                "selected_max_output_bytes": balanced_policy["max_output_bytes"],
            },
        }
    if target_bytes < space_bytes:
        return {
            "status": "infeasible",
            "policy": space_policy,
            "transformation": space_transform,
            "target": {
                **base_target,
                "selection_method": "space_profile_is_strongest_named_policy",
                "selected_max_output_bytes": space_policy["max_output_bytes"],
                "remaining_protected_bytes": space_bytes - target_bytes,
            },
        }
    if target_bytes >= balanced_bytes:
        return {
            "status": "ready_for_review",
            "policy": balanced_policy,
            "transformation": balanced_transform,
            "target": {
                **base_target,
                "selection_method": "balanced_profile_satisfies_target",
                "selected_max_output_bytes": balanced_policy["max_output_bytes"],
            },
        }

    low = int(space_policy["max_output_bytes"])
    high = int(balanced_policy["max_output_bytes"])
    best: tuple[dict[str, Any], dict[str, Any], int] | None = None
    thresholds = list(range(low, high + 1, 1024))
    if thresholds[-1] != high:
        thresholds.append(high)
    for threshold in thresholds:
        prefix_bytes, suffix_bytes = interpolate_preview_bytes(threshold)
        candidate_policy = {
            "profile": "custom",
            "scrub_images": True,
            "max_output_bytes": threshold,
            "prefix_bytes": prefix_bytes,
            "suffix_bytes": suffix_bytes,
        }
        candidate_bytes, candidate_transform = render(candidate_policy)
        if candidate_bytes <= target_bytes:
            best = (candidate_policy, candidate_transform, candidate_bytes)
    if best is None:
        return {
            "status": "infeasible",
            "policy": space_policy,
            "transformation": space_transform,
            "target": {
                **base_target,
                "selection_method": "no_threshold_reached_target",
                "selected_max_output_bytes": space_policy["max_output_bytes"],
                "remaining_protected_bytes": space_bytes - target_bytes,
            },
        }
    policy, transformation, candidate_bytes = best
    return {
        "status": "ready_for_review",
        "policy": policy,
        "transformation": transformation,
        "target": {
            **base_target,
            "selection_method": "deterministic_scan_between_balanced_and_space",
            "selected_max_output_bytes": policy["max_output_bytes"],
            "selected_candidate_bytes": candidate_bytes,
        },
    }


def transform_lines(
    records: list[dict[str, Any]],
    raw_lines: list[bytes],
    protected_from: int,
    max_output_bytes: int | None = None,
    prefix_bytes: int | None = None,
    suffix_bytes: int | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if policy is None:
        policy = resolve_profile_policy(
            "custom",
            max_output_bytes,
            prefix_bytes,
            suffix_bytes,
        )
    elif any(value is not None for value in (max_output_bytes, prefix_bytes, suffix_bytes)):
        raise ValueError("manual output thresholds cannot be combined with policy")
    policy = dict(policy)
    if policy.get("profile") not in PROFILE_POLICIES and policy.get("profile") != "custom":
        raise ValueError("invalid transformation policy profile")
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
        if policy.get("scrub_images"):
            scrubbed_output, image_count = scrub_image_nodes(payload["output"])
        else:
            scrubbed_output, image_count = payload["output"], 0
        if policy.get("max_output_bytes") is None:
            truncated_output, did_truncate = scrubbed_output, False
        else:
            truncated_output, did_truncate = truncate_output(
                scrubbed_output,
                int(policy["max_output_bytes"]),
                int(policy["prefix_bytes"]),
                int(policy["suffix_bytes"]),
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
        "policy": policy,
    }


def write_candidate(
    source: Path,
    candidate: Path,
    records: list[dict[str, Any]],
    raw_lines: list[bytes],
    protected_from: int,
    max_output_bytes: int | None = None,
    prefix_bytes: int | None = None,
    suffix_bytes: int | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transformed = transform_lines(
        records,
        raw_lines,
        protected_from,
        max_output_bytes,
        prefix_bytes,
        suffix_bytes,
        policy=policy,
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
        "policy": transformed["policy"],
        "candidate_bytes": candidate.stat().st_size,
    }


def build_residual_risk(
    requested_profile: str,
    policy: dict[str, Any],
    protected_region: dict[str, Any],
    summary: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    items: list[str] = []
    if status == "infeasible":
        items.append("the requested target cannot be reached without changing protected content")
    elif status == "no_change":
        items.append("no source content would be changed by this candidate")
    else:
        if summary.get("image_payloads_cleared", 0):
            items.append("old tool-output image cache payloads are replaced by markers and are not recoverable from the candidate")
        if summary.get("truncated_outputs", 0):
            items.append("middle sections of oversized old tool outputs are omitted from the candidate")
        if not items:
            items.append("no eligible old tool-output payload was changed")
    if status not in {"infeasible", "no_change"}:
        items.append("protected recent records, visible messages, user images, and structural IDs remain unchanged")
    return {
        "profile": requested_profile,
        "level": "blocked" if status in {"blocked", "infeasible"} else ("moderate" if summary.get("truncated_outputs", 0) else "low"),
        "items": items,
        "selected_policy": copy.deepcopy(policy),
        "protected_from_line": protected_region.get("from_line"),
        "selected_boundary_lines": list(protected_region.get("selected_boundary_lines", [])),
        "mitigation": "apply creates a byte-for-byte original backup before replacement",
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


def build_intent_profile(
    stats: dict[str, Any],
    *,
    problem: str | None = None,
    retention_priority: str | None = None,
    allowed_strength: str | None = None,
    target_bytes: int | None = None,
) -> dict[str, Any]:
    valid_problems = {"image_cache", "oversized_output", "overall_size", "context_pressure"}
    valid_priorities = {"recent_content", "visible_messages", "user_images", "structural_fidelity"}
    valid_strengths = {"cache", "balanced", "space"}
    if problem is not None and problem not in valid_problems:
        raise ValueError("invalid problem")
    if retention_priority is not None and retention_priority not in valid_priorities:
        raise ValueError("invalid retention_priority")
    if allowed_strength is not None and allowed_strength not in valid_strengths:
        raise ValueError("invalid allowed_strength")
    if target_bytes is not None and target_bytes < 1:
        raise ValueError("target_bytes must be at least 1")

    assumptions: list[str] = []
    if problem is None:
        if stats.get("image_payload_count", 0):
            problem = "image_cache"
        elif stats.get("tool_output_bytes", 0) > DEFAULT_MAX_OUTPUT_BYTES:
            problem = "oversized_output"
        else:
            problem = "overall_size"
        assumptions.append(f"inferred problem from inspect metrics: {problem}")
    if retention_priority is None:
        retention_priority = "recent_content"
        assumptions.append("preserve recent content as the default retention priority")
    if allowed_strength is None:
        allowed_strength = "balanced"
        assumptions.append("use balanced as the default allowed cleanup strength")
    evidence = {
        "record_count": stats.get("record_count", 0),
        "tool_output_bytes": stats.get("tool_output_bytes", 0),
        "tool_output_count": stats.get("tool_output_count", 0),
        "image_payload_count": stats.get("image_payload_count", 0),
        "user_image_payload_count": stats.get("user_image_payload_count", 0),
        "logical_compaction_count": stats.get("logical_compaction_count", 0),
    }
    return {
        "problem": problem,
        "retention_priority": retention_priority,
        "allowed_strength": allowed_strength,
        "target_bytes": target_bytes,
        "assumptions": assumptions,
        "evidence": evidence,
    }


def validate_intent_profile(intent_profile: Any) -> list[str]:
    if not isinstance(intent_profile, dict):
        return ["intent profile metadata is missing; regenerate the plan"]
    required = {
        "problem",
        "retention_priority",
        "allowed_strength",
        "target_bytes",
        "assumptions",
        "evidence",
    }
    missing = sorted(required - set(intent_profile))
    if missing:
        return ["intent profile is incomplete: " + ", ".join(missing)]
    errors: list[str] = []
    if intent_profile["problem"] not in {"image_cache", "oversized_output", "overall_size", "context_pressure"}:
        errors.append("intent profile problem is invalid")
    if intent_profile["retention_priority"] not in {"recent_content", "visible_messages", "user_images", "structural_fidelity"}:
        errors.append("intent profile retention_priority is invalid")
    if intent_profile["allowed_strength"] not in {"cache", "balanced", "space"}:
        errors.append("intent profile allowed_strength is invalid")
    target_bytes = intent_profile["target_bytes"]
    if target_bytes is not None and (not isinstance(target_bytes, int) or isinstance(target_bytes, bool) or target_bytes < 1):
        errors.append("intent profile target_bytes is invalid")
    if not isinstance(intent_profile["assumptions"], list):
        errors.append("intent profile assumptions must be a list")
    if not isinstance(intent_profile["evidence"], dict):
        errors.append("intent profile evidence must be an object")
    return errors


def validate_plan_semantics(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    requested_profile = plan.get("requested_profile")
    intent = plan.get("intent_profile")
    target = plan.get("target")
    intent_target = intent.get("target_bytes") if isinstance(intent, dict) else None
    if requested_profile == "target":
        if not isinstance(target, dict):
            errors.append("target profile requires target metadata")
        if intent_target is None:
            errors.append("target profile requires intent_profile.target_bytes")
        elif isinstance(target, dict) and target.get("target_bytes") != intent_target:
            errors.append("target metadata does not match intent profile")
    elif intent_target is not None:
        errors.append("target_bytes requires requested_profile=target")
    elif target is not None:
        errors.append("target metadata requires requested_profile=target")
    return errors


def validate_residual_risk(plan: dict[str, Any]) -> list[str]:
    risk = plan.get("residual_risk")
    if not isinstance(risk, dict):
        return ["residual risk metadata is missing; regenerate the plan"]
    expected = build_residual_risk(
        str(plan.get("requested_profile")),
        plan.get("policy") if isinstance(plan.get("policy"), dict) else {},
        plan.get("protected_region") if isinstance(plan.get("protected_region"), dict) else {},
        plan.get("summary") if isinstance(plan.get("summary"), dict) else {},
        str(plan.get("status")),
    )
    if risk != expected:
        return ["residual risk metadata does not match the plan"]
    return []


def build_plan(
    source: Path,
    report_dir: Path,
    *,
    recent_records: int = DEFAULT_RECENT_RECORDS,
    recent_compactions: int = DEFAULT_RECENT_COMPACTIONS,
    max_output_bytes: int | None = None,
    prefix_bytes: int | None = None,
    suffix_bytes: int | None = None,
    profile: str | None = None,
    intent_profile: dict[str, Any] | None = None,
    target_bytes: int | None = None,
) -> dict[str, Any]:
    if profile is None:
        profile = "balanced" if all(value is None for value in (max_output_bytes, prefix_bytes, suffix_bytes)) else "custom"
    requested_profile = profile
    requested_policy = resolve_plan_profile(
        profile,
        target_bytes=target_bytes,
        max_output_bytes=max_output_bytes,
        prefix_bytes=prefix_bytes,
        suffix_bytes=suffix_bytes,
    )
    validate_cleanup_options(
        recent_records,
        DEFAULT_MAX_OUTPUT_BYTES,
        DEFAULT_PREFIX_BYTES,
        DEFAULT_SUFFIX_BYTES,
        recent_compactions,
    )
    source = validate_session_path(source)
    report_dir = report_dir.expanduser().resolve()
    records, raw_lines, errors = parse_jsonl(source)
    if not records:
        errors.append({"line": 0, "error": "empty_session"})
    source_fingerprint = fingerprint(source)
    stats = collect_stats(source, records, errors)
    protected_region = boundary_metadata(records, recent_records, recent_compactions)
    protected_from = protected_region["from_line"]
    target_selection: dict[str, Any] | None = None
    if requested_profile == "target":
        target_selection = select_target_policy(records, raw_lines, protected_from, target_bytes or 0)
        policy = target_selection["policy"]
        selection_status = target_selection["status"]
    else:
        policy = requested_policy
        selection_status = "ready_for_review"
    effective_intent = (
        copy.deepcopy(intent_profile)
        if intent_profile is not None
        else build_intent_profile(stats, target_bytes=target_bytes)
    )
    if target_bytes is not None and effective_intent.get("target_bytes") != target_bytes:
        raise ValueError("intent profile target_bytes does not match plan target_bytes")
    plan_id = uuid.uuid4().hex
    candidate = report_dir / f"candidate-{plan_id}.jsonl"
    transformation = write_candidate(
        source,
        candidate,
        records,
        raw_lines,
        protected_from,
        policy=policy,
    )
    candidate_fingerprint = fingerprint(candidate)
    plan = {
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "candidate_kind": "session_cleanup",
        "requested_profile": requested_profile,
        "status": selection_status if not errors else "blocked",
        "created_at": utc_now(),
        "source": source_fingerprint,
        "candidate": candidate_fingerprint,
        "candidate_path": str(candidate),
        "report_path": str(report_dir / f"plan-{plan_id}.json"),
        "audit_path": str(report_dir / f"audit-{plan_id}.json"),
        "session_id": stats["session_id"],
        "source_stats": stats,
        "intent_profile": effective_intent,
        "policy": policy,
        "target": target_selection["target"] if target_selection is not None else None,
        "locks": find_locks(source),
        "protected_region": {
            **protected_region,
            "rules": [
                "preserve all records from the selected logical compaction boundary",
                "preserve all user and assistant visible messages byte-for-byte",
                "preserve all non-tool-output records and tool call IDs",
                "preserve images embedded in user messages",
            ],
        },
        "transformation": {
            "old_tool_output_only": True,
            "max_output_bytes": policy.get("max_output_bytes"),
            "prefix_bytes": policy.get("prefix_bytes"),
            "suffix_bytes": policy.get("suffix_bytes"),
            "policy": policy,
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
    elif selection_status == "infeasible":
        plan["blocking_reasons"] = ["target_size_infeasible"]
    elif selection_status == "no_change":
        plan["blocking_reasons"] = ["target_already_satisfied"]
    plan["residual_risk"] = build_residual_risk(
        requested_profile,
        policy,
        plan["protected_region"],
        plan["summary"],
        plan["status"],
    )
    plan["plan_digest"] = self_digest(plan, "plan_digest")
    write_json(Path(plan["report_path"]), plan)
    return plan


def build_plan_set(
    source: Path,
    report_dir: Path,
    *,
    profiles: Iterable[str] = ("cache", "balanced", "space"),
    intent_profile: dict[str, Any] | None = None,
    recent_records: int = DEFAULT_RECENT_RECORDS,
    recent_compactions: int = DEFAULT_RECENT_COMPACTIONS,
) -> dict[str, Any]:
    source = validate_session_path(source)
    report_dir = report_dir.expanduser().resolve()
    profile_list = list(profiles)
    if not profile_list:
        raise ValueError("at least one cleanup profile is required")
    if len(profile_list) != len(set(profile_list)):
        raise ValueError("cleanup profiles must be unique")
    for profile in profile_list:
        if profile not in PROFILE_POLICIES:
            raise ValueError("plan sets support cache, balanced, and space profiles")

    records, _, errors = parse_jsonl(source)
    if not records:
        errors.append({"line": 0, "error": "empty_session"})
    source_snapshot = fingerprint(source)
    stats = collect_stats(source, records, errors)
    resolved_intent = copy.deepcopy(intent_profile) if intent_profile is not None else build_intent_profile(stats)
    intent_errors = validate_intent_profile(resolved_intent)
    if intent_errors:
        raise ValueError("invalid intent profile: " + "; ".join(intent_errors))

    plan_set_id = uuid.uuid4().hex
    candidates: list[dict[str, Any]] = []
    blocked = bool(errors)
    for profile in profile_list:
        plan = build_plan(
            source,
            report_dir,
            recent_records=recent_records,
            recent_compactions=recent_compactions,
            profile=profile,
            intent_profile=resolved_intent,
        )
        plan["plan_set_id"] = plan_set_id
        plan["plan_digest"] = self_digest(plan, "plan_digest")
        write_json(Path(plan["report_path"]), plan)
        if plan["status"] != "ready_for_review":
            blocked = True
        if plan["status"] != "ready_for_review" or plan["summary"]["changed_records"] == 0:
            continue
        candidates.append(
            {
                "plan_id": plan["plan_id"],
                "plan_path": plan["report_path"],
                "candidate_path": plan["candidate_path"],
                "audit_path": plan["audit_path"],
                "source_sha256": plan["source"]["sha256"],
                "requested_profile": plan["requested_profile"],
                "status": plan["status"],
                "policy": copy.deepcopy(plan["policy"]),
                "original_bytes": plan["summary"]["original_bytes"],
                "candidate_bytes": plan["summary"]["candidate_bytes"],
                "bytes_saved": plan["summary"]["bytes_saved"],
                "changed_records": plan["summary"]["changed_records"],
                "image_payloads_cleared": plan["summary"]["image_payloads_cleared"],
                "truncated_outputs": plan["summary"]["truncated_outputs"],
                "protected_region": copy.deepcopy(plan["protected_region"]),
                "residual_risk": copy.deepcopy(plan["residual_risk"]),
                "audit_status": "pending",
            }
        )

    if fingerprint(source)["sha256"] != source_snapshot["sha256"]:
        raise ValueError("source changed while generating candidate plans")
    plan_set_path = report_dir / f"plan-set-{plan_set_id}.json"
    if blocked:
        status = "blocked"
    elif not candidates:
        status = "no_change"
    else:
        status = "ready_for_review"
    plan_set = {
        "plan_set_version": 1,
        "plan_set_id": plan_set_id,
        "status": status,
        "created_at": utc_now(),
        "source": source_snapshot,
        "intent_profile": resolved_intent,
        "requested_profiles": profile_list,
        "candidates": candidates,
        "audit_status": "pending",
    }
    plan_set["plan_set_path"] = str(plan_set_path)
    plan_set["plan_set_digest"] = self_digest(plan_set, "plan_set_digest")
    write_json(plan_set_path, plan_set)
    return plan_set


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
    errors.extend(validate_plan_policy(plan))
    changed_lines = {
        item["line"] for item in plan.get("transformation", {}).get("changed_lines", [])
    }
    protected_from, boundary_errors, expected_boundary = plan_boundary_metadata(
        source_records, plan
    )
    errors.extend(boundary_errors)
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
        "boundary": expected_boundary,
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
    protected_from, boundary_errors, expected_boundary = plan_boundary_metadata(
        source_records, plan
    )
    errors: list[str] = list(boundary_errors)
    errors.extend(validate_plan_policy(plan))
    try:
        policy = plan.get("policy")
        if not isinstance(policy, dict):
            raise ValueError("policy metadata is missing")
        else:
            profile_name = str(policy.get("profile"))
            if profile_name in PROFILE_POLICIES:
                resolved_policy = resolve_profile_policy(profile_name)
                policy = resolved_policy
            else:
                policy = resolve_profile_policy(
                    profile_name,
                    policy.get("max_output_bytes"),
                    policy.get("prefix_bytes"),
                    policy.get("suffix_bytes"),
                )
        if plan.get("requested_profile") == "target":
            target = plan.get("target")
            if not isinstance(target, dict):
                raise ValueError("target metadata is missing")
            target_bytes = target.get("target_bytes")
            if plan.get("intent_profile", {}).get("target_bytes") != target_bytes:
                errors.append("target metadata does not match intent profile")
            selection = select_target_policy(
                source_records,
                source_lines,
                protected_from,
                int(target_bytes),
            )
            if selection["status"] != plan.get("status"):
                errors.append("target selection status changed")
            if selection["policy"] != plan.get("policy"):
                errors.append("target policy changed since plan generation")
            if selection["target"] != target:
                errors.append("target metadata changed since plan generation")
        region = plan.get("protected_region", {})
        recent_records = int(region.get("fallback_recent_records", DEFAULT_RECENT_RECORDS))
        recent_compactions = int(
            region.get("requested_logical_compactions", DEFAULT_RECENT_COMPACTIONS)
        )
        validate_cleanup_options(
            recent_records,
            int(policy.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES),
            int(policy.get("prefix_bytes") or DEFAULT_PREFIX_BYTES),
            int(policy.get("suffix_bytes") or DEFAULT_SUFFIX_BYTES),
            recent_compactions,
        )
        expected = transform_lines(
            source_records,
            source_lines,
            protected_from,
            policy=policy,
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
        "boundary": expected_boundary,
        "transformation": options,
        "policy": policy,
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
    protected_from, boundary_errors, expected_boundary = plan_boundary_metadata(
        source_records, plan
    )
    errors.extend(boundary_errors)
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
        "boundary": expected_boundary,
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
    plan_errors.extend(validate_intent_profile(plan.get("intent_profile")))
    plan_errors.extend(validate_plan_semantics(plan))
    plan_errors.extend(validate_residual_risk(plan))
    if plan.get("plan_digest") != self_digest(plan, "plan_digest"):
        plan_errors.append("plan digest changed")
    if plan.get("status") != "ready_for_review":
        plan_errors.append("plan is not ready for review")
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


def audit_plan_set(plan_set_path: Path) -> dict[str, Any]:
    plan_set_path = plan_set_path.expanduser().resolve()
    plan_set = read_json(plan_set_path)
    errors: list[str] = []
    if plan_set.get("plan_set_version") != 1:
        errors.append("unsupported plan-set version; regenerate the plan set")
    if plan_set.get("plan_set_digest") != self_digest(plan_set, "plan_set_digest"):
        errors.append("plan-set digest changed")
    candidates = plan_set.get("candidates")
    if not isinstance(candidates, list):
        errors.append("plan-set candidates are missing")
        candidates = []
    source = plan_set.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
        errors.append("plan-set source metadata is missing")
        source = {}
    requested_profiles = plan_set.get("requested_profiles")
    if not isinstance(requested_profiles, list) or not requested_profiles:
        errors.append("plan-set requested profiles are missing")
        requested_profiles = []
    requested_profile_set = set(requested_profiles)
    candidate_audits: list[dict[str, Any]] = []
    seen_plan_ids: set[str] = set()
    seen_profiles: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            errors.append("plan-set candidate entry is not an object")
            continue
        plan_id = item.get("plan_id")
        plan_path_value = item.get("plan_path")
        if not isinstance(plan_id, str) or not isinstance(plan_path_value, str):
            errors.append("plan-set candidate identity is incomplete")
            continue
        if plan_id in seen_plan_ids:
            errors.append(f"duplicate candidate plan_id: {plan_id}")
            continue
        seen_plan_ids.add(plan_id)
        candidate_plan_path = Path(plan_path_value).expanduser().resolve()
        try:
            candidate_plan = read_json(candidate_plan_path)
            if candidate_plan.get("plan_id") != plan_id:
                raise ValueError("candidate plan_id does not match plan-set entry")
            if candidate_plan.get("plan_set_id") != plan_set.get("plan_set_id"):
                raise ValueError("candidate is bound to a different plan set")
            if candidate_plan.get("source", {}).get("sha256") != source.get("sha256"):
                raise ValueError("candidate source hash differs from plan-set source")
            if item.get("source_sha256") != candidate_plan.get("source", {}).get("sha256"):
                errors.append(f"candidate {plan_id}: plan-set index source hash does not match candidate")
            if item.get("audit_path") != candidate_plan.get("audit_path"):
                errors.append(f"candidate {plan_id}: plan-set index audit path does not match candidate")
            if item.get("candidate_path") != candidate_plan.get("candidate_path"):
                errors.append(f"candidate {plan_id}: plan-set index candidate path does not match candidate")
            for field in ("requested_profile", "status", "protected_region", "residual_risk"):
                if item.get(field) != candidate_plan.get(field):
                    errors.append(f"candidate {plan_id}: plan-set index {field} does not match candidate")
            if item.get("original_bytes") != candidate_plan.get("summary", {}).get("original_bytes"):
                errors.append(f"candidate {plan_id}: plan-set index original bytes do not match candidate")
            if item.get("policy") != candidate_plan.get("policy"):
                errors.append(f"candidate {plan_id}: plan-set index policy does not match candidate")
            profile = candidate_plan.get("policy", {}).get("profile")
            if profile not in requested_profile_set:
                errors.append(f"candidate {plan_id}: profile is not requested by plan set")
            elif profile in seen_profiles:
                errors.append(f"candidate {plan_id}: duplicate profile in plan set")
            elif isinstance(profile, str):
                seen_profiles.add(profile)
            if candidate_plan.get("source", {}).get("path") != source.get("path"):
                errors.append(f"candidate {plan_id}: candidate source path differs from plan-set source")
            if candidate_plan.get("intent_profile") != plan_set.get("intent_profile"):
                errors.append(f"candidate {plan_id}: intent profile differs from plan set")
            audit = audit_plan(candidate_plan_path)
        except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            audit = {
                "plan_id": plan_id,
                "plan_path": str(candidate_plan_path),
                "status": "fail",
                "errors": [str(exc)],
            }
        candidate_audits.append(
            {
                "plan_id": plan_id,
                "plan_path": str(candidate_plan_path),
                "audit_path": item.get("audit_path"),
                "status": audit.get("status", "fail"),
                "errors": audit.get("errors", []),
                "audit_digest": audit.get("audit_digest"),
            }
        )
        if audit.get("status") != "pass":
            errors.extend(
                f"candidate {plan_id}: {error}"
                for error in audit.get("errors", ["candidate audit failed"])
            )

    status = "pass" if not errors and candidate_audits else "fail"
    result = {
        "plan_set_audit_version": 1,
        "audit_id": uuid.uuid4().hex,
        "created_at": utc_now(),
        "plan_set_id": plan_set.get("plan_set_id"),
        "plan_set_path": str(plan_set_path),
        "source_sha256": source.get("sha256"),
        "status": status,
        "candidate_audits": candidate_audits,
        "errors": errors,
    }
    result["audit_digest"] = self_digest(result, "audit_digest")
    updated = copy.deepcopy(plan_set)
    updated["audit_status"] = status
    updated["candidate_audits"] = candidate_audits
    updated["audit_id"] = result["audit_id"]
    updated["audit_digest"] = result["audit_digest"]
    updated["plan_set_digest"] = self_digest(updated, "plan_set_digest")
    write_json(plan_set_path, updated)
    result["plan_set_digest"] = updated["plan_set_digest"]
    return result


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
    if "plan_set_version" in plan:
        raise ValueError("apply requires one candidate plan, not a plan set")
    if plan.get("plan_version") != PLAN_VERSION:
        raise ValueError("unsupported plan version; regenerate the plan")
    policy_errors = validate_plan_policy(plan)
    if policy_errors:
        raise ValueError("invalid cleanup policy: " + "; ".join(policy_errors))
    intent_errors = validate_intent_profile(plan.get("intent_profile"))
    if intent_errors:
        raise ValueError("invalid intent profile: " + "; ".join(intent_errors))
    semantic_errors = validate_plan_semantics(plan)
    if semantic_errors:
        raise ValueError("invalid cleanup plan semantics: " + "; ".join(semantic_errors))
    residual_risk_errors = validate_residual_risk(plan)
    if residual_risk_errors:
        raise ValueError("invalid residual risk metadata: " + "; ".join(residual_risk_errors))
    if plan.get("status") != "ready_for_review":
        raise ValueError("plan is not ready for review")
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


def parse_backup_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def normalize_backup_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("backup evaluation time must include a timezone")
    return current.astimezone(timezone.utc)


def backup_directory_size(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def backup_age_days(entry: dict[str, Any], now: datetime) -> int | None:
    created = parse_backup_timestamp(entry.get("created_at"))
    if created is None:
        return None
    seconds = (now - created).total_seconds()
    return max(0, int(seconds // 86400))


def backup_entry_sort_key(entry: dict[str, Any]) -> tuple[float, str, str]:
    created = parse_backup_timestamp(entry.get("created_at"))
    timestamp = created.timestamp() if created is not None else float("-inf")
    return timestamp, str(entry.get("created_at", "")), str(entry.get("path", ""))


def annotate_backup_entries(
    entries: list[dict[str, Any]],
    *,
    keep: int,
    older_than_days: int | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    valid = [
        entry
        for entry in entries
        if entry.get("status") == "success" and entry.get("integrity") == "valid"
    ]
    valid.sort(key=backup_entry_sort_key, reverse=True)
    retained_paths = {
        str(Path(entry.get("path", "")).resolve()) for entry in valid[:keep] if entry.get("path")
    }
    for entry in entries:
        path = Path(entry.get("path", "")).resolve() if entry.get("path") else None
        entry["size_bytes"] = backup_directory_size(path) if path else 0
        entry["age_days"] = backup_age_days(entry, now)
        entry["deletion_eligible"] = False
        if entry.get("status") != "success" or entry.get("integrity") != "valid":
            entry["deletion_reason"] = "backup is not a valid successful recovery point"
        elif path is None or str(path) in retained_paths:
            entry["deletion_reason"] = "kept recovery point"
        elif entry["age_days"] is None:
            entry["deletion_reason"] = "invalid timestamp; preserved"
        elif older_than_days is not None and entry["age_days"] < older_than_days:
            entry["deletion_reason"] = f"younger than {older_than_days} days"
        else:
            entry["deletion_eligible"] = True
            entry["deletion_reason"] = "eligible after keep and age filters"
    return entries


def list_backups(
    backup_root: Path,
    session_id: str | None = None,
    *,
    keep: int = 2,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    backup_root = backup_root.expanduser().resolve()
    if session_id is not None:
        validate_backup_session_id(session_id)
    if not backup_root.exists():
        return []
    roots = (
        [backup_root / session_id]
        if session_id
        else [
            path
            for path in backup_root.iterdir()
            if path.is_dir() and not path.is_symlink() and path.name not in BACKUP_INTERNAL_DIRS
        ]
    )
    result: list[dict[str, Any]] = []
    for session_root in roots:
        if session_root.is_symlink():
            continue
        if not session_root.is_dir() or session_root.name in BACKUP_INTERNAL_DIRS:
            continue
        for batch in session_root.iterdir():
            manifest_path = batch / "manifest.json"
            if not batch.is_dir():
                continue
            if batch.is_symlink():
                result.append(
                    {
                        "path": str(batch.absolute()),
                        "session_id": session_root.name,
                        "status": "unknown",
                        "integrity": "unknown",
                        "reason": "symlinked backup directory is preserved",
                    }
                )
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
    evaluated_at = normalize_backup_now(now)
    result.sort(key=backup_entry_sort_key, reverse=True)
    return annotate_backup_entries(
        result,
        keep=keep,
        older_than_days=older_than_days,
        now=evaluated_at,
    )


def checked_backup_file(backup_dir: Path, filename: str) -> Path:
    path = backup_dir / filename
    if path.is_symlink():
        raise ValueError(f"backup file must not be a symlink: {path}")
    try:
        ensure_inside(backup_dir, path.resolve())
    except ValueError as error:
        raise ValueError(f"backup file escapes its batch directory: {path}") from error
    if not path.is_file():
        raise ValueError(f"backup file is missing: {path}")
    return path


def backup_integrity(entry: dict[str, Any], backup_root: Path, session_id: str) -> tuple[bool, str]:
    raw_path = Path(entry.get("path", ""))
    if raw_path.is_symlink():
        return False, "backup directory is a symlink"
    path = raw_path.resolve()
    try:
        ensure_inside(backup_root, path)
    except ValueError as error:
        return False, str(error)
    if entry.get("session_id") != session_id or entry.get("status") != "success":
        return False, "not a successful backup for this session"
    if entry.get("backup_version") != BACKUP_VERSION:
        return False, f"unsupported backup version: {entry.get('backup_version')!r}"
    if not path.is_dir():
        return False, "backup files are incomplete"
    try:
        manifest_path = checked_backup_file(path, "manifest.json")
        original_path = checked_backup_file(path, "original.jsonl")
        manifest = read_json(manifest_path)
        if manifest.get("backup_version") != BACKUP_VERSION:
            return False, f"unsupported backup version: {manifest.get('backup_version')!r}"
        if manifest.get("backup_id") != entry.get("backup_id"):
            return False, "manifest identity changed"
        if manifest.get("manifest_digest") != self_digest(manifest, "manifest_digest"):
            return False, "manifest digest mismatch"
        source_path = manifest.get("source_path")
        if not isinstance(source_path, str):
            return False, "manifest source path is missing"
        validate_session_path(Path(source_path))
        if sha256_file(original_path) != manifest.get("original_sha256"):
            return False, "original backup hash mismatch"
        records, _, errors = parse_jsonl(original_path)
        if not records or errors:
            return False, "original backup is not valid JSONL"
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return False, str(error)
    return True, "ok"


def prune_snapshot(
    entries: list[dict[str, Any]],
    backup_root: Path,
    session_id: str,
    keep: int,
    *,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    evaluation_now = normalize_backup_now(now)
    valid: list[dict[str, Any]] = []
    preserved: list[str] = []
    preserved_reasons: list[str] = []
    invalid_reasons: dict[str, str] = {}
    for entry in entries:
        path_value = entry.get("path")
        if not path_value:
            continue
        path = str(Path(path_value).resolve())
        if entry.get("status") == "success":
            is_valid, reason = backup_integrity(entry, backup_root, session_id)
            if is_valid:
                valid.append(entry)
            else:
                preserved.append(path)
                invalid_reasons[path] = reason
                preserved_reasons.append(f"{path}: {reason}")
        else:
            preserved.append(path)
            preserved_reasons.append(f"{path}: backup status is not successful")
    valid.sort(key=backup_entry_sort_key, reverse=True)
    retained = valid[:keep]
    retained_paths = {str(Path(entry["path"]).resolve()) for entry in retained}
    candidates: list[dict[str, Any]] = []
    for entry in valid[keep:]:
        path = str(Path(entry["path"]).resolve())
        age_days = backup_age_days(entry, evaluation_now)
        if age_days is None:
            preserved.append(path)
            preserved_reasons.append(f"{path}: invalid timestamp; preserved")
            continue
        if older_than_days is not None and age_days < older_than_days:
            preserved.append(path)
            preserved_reasons.append(f"{path}: younger than {older_than_days} days")
            continue
        candidates.append(
            {
                "path": path,
                "backup_id": entry.get("backup_id"),
                "created_at": entry.get("created_at"),
                "age_days": age_days,
                "size_bytes": int(entry.get("size_bytes", backup_directory_size(Path(path)))),
            }
        )
    snapshot = []
    for entry in entries:
        if not entry.get("path"):
            continue
        entry_path = Path(entry["path"]).resolve()
        manifest_path = entry_path / "manifest.json"
        snapshot.append(
            {
                "path": str(entry_path),
                "backup_id": entry.get("backup_id"),
                "status": entry.get("status"),
                "integrity": entry.get("integrity"),
                "created_at": entry.get("created_at"),
                "age_days": backup_age_days(entry, evaluation_now),
                "size_bytes": int(entry.get("size_bytes", backup_directory_size(entry_path))),
                "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
                "original_sha256": entry.get("original_sha256"),
            }
        )
    candidate_paths = [item["path"] for item in candidates]
    return {
        "session_id": session_id,
        "keep_successful": keep,
        "older_than_days": older_than_days,
        "evaluation_now": evaluation_now.isoformat().replace("+00:00", "Z"),
        "retained_valid_successful": len(retained),
        "retained_paths": sorted(retained_paths),
        "candidates": candidates,
        "candidate_paths": candidate_paths,
        "reclaimable_bytes": sum(item["size_bytes"] for item in candidates),
        "preserved_paths": sorted(set(preserved)),
        "preserved_reasons": sorted(set(preserved_reasons)),
        "invalid_reasons": invalid_reasons,
        "snapshot": snapshot,
    }


def prune_backups(
    backup_root: Path,
    session_id: str,
    *,
    keep: int = 2,
    confirm: str | bool | None = None,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    validate_backup_session_id(session_id)
    backup_root = backup_root.expanduser().resolve()
    evaluation_now = normalize_backup_now(now)
    entries = list_backups(
        backup_root,
        session_id,
        keep=keep,
        older_than_days=older_than_days,
        now=evaluation_now,
    )
    if confirm is True:
        raise ValueError("prune confirmation must be the preview_id returned by a prior preview")
    if isinstance(confirm, str):
        validate_preview_id(confirm)
        preview_path = backup_root / ".prune-previews" / f"{confirm}.json"
        if not preview_path.is_file():
            raise ValueError("unknown or expired prune preview_id")
        preview = read_json(preview_path)
        if preview.get("preview_version") != 2:
            raise ValueError("unsupported backup preview version; create a new preview")
        if preview.get("preview_id") != confirm:
            raise ValueError("prune preview identity mismatch")
        if preview.get("preview_digest") != self_digest(preview, "preview_digest"):
            raise ValueError("prune preview was changed after creation")
        if older_than_days != preview.get("older_than_days"):
            raise ValueError("backup age filter changed after preview; create a new preview")
        preview_now = parse_backup_timestamp(preview.get("evaluation_now")) or evaluation_now
        preview_age = preview.get("older_than_days")
        snapshot = prune_snapshot(
            list_backups(
                backup_root,
                session_id,
                keep=keep,
                older_than_days=preview_age,
                now=preview_now,
            ),
            backup_root,
            session_id,
            keep,
            older_than_days=preview_age,
            now=preview_now,
        )
        snapshot_fields = (
            "session_id",
            "keep_successful",
            "older_than_days",
            "evaluation_now",
            "retained_valid_successful",
            "retained_paths",
            "candidates",
            "candidate_paths",
            "reclaimable_bytes",
            "preserved_paths",
            "preserved_reasons",
            "invalid_reasons",
            "snapshot",
        )
        if any(snapshot.get(field) != preview.get(field) for field in snapshot_fields):
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
            "reclaimed_bytes": int(preview.get("reclaimable_bytes", 0)),
        }
    snapshot = prune_snapshot(
        entries,
        backup_root,
        session_id,
        keep,
        older_than_days=older_than_days,
        now=evaluation_now,
    )
    preview_id = uuid.uuid4().hex
    preview = {
        "preview_version": 2,
        "preview_id": preview_id,
        "created_at": utc_now(),
        **snapshot,
    }
    preview["preview_digest"] = self_digest(preview, "preview_digest")
    preview_path = backup_root / ".prune-previews" / f"{preview_id}.json"
    write_json(preview_path, preview)
    return {
        "status": "preview",
        "preview_version": preview["preview_version"],
        "preview_id": preview_id,
        "preview_path": str(preview_path),
        "session_id": session_id,
        "keep_successful": keep,
        "older_than_days": older_than_days,
        "evaluation_now": snapshot["evaluation_now"],
        "retained_valid_successful": snapshot["retained_valid_successful"],
        "retained_paths": snapshot["retained_paths"],
        "delete_count": len(snapshot["candidate_paths"]),
        "delete_paths": snapshot["candidate_paths"],
        "candidates": snapshot["candidates"],
        "reclaimable_bytes": snapshot["reclaimable_bytes"],
        "preserved_paths": snapshot["preserved_paths"],
        "preserved_reasons": snapshot["preserved_reasons"],
        "invalid_reasons": snapshot["invalid_reasons"],
        "snapshot": snapshot["snapshot"],
        "preview_digest": preview["preview_digest"],
    }


def restore_backup(
    backup_dir: Path,
    confirmation: str,
    target: Path | None = None,
    *,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    raw_backup_dir = backup_dir.expanduser()
    raw_backup_root = (backup_root or default_backup_root()).expanduser()
    if raw_backup_dir.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    if raw_backup_root.is_symlink():
        raise ValueError("backup root must not be a symlink")
    if raw_backup_dir.parent.is_symlink():
        raise ValueError("backup session directory must not be a symlink")
    managed_root = raw_backup_root.resolve()
    backup_dir = raw_backup_dir.resolve()
    ensure_inside(managed_root, backup_dir)
    if backup_dir.parent.parent != managed_root:
        raise ValueError("backup directory must be directly under backup_root/session_id")
    manifest_path = checked_backup_file(backup_dir, "manifest.json")
    original = checked_backup_file(backup_dir, "original.jsonl")
    manifest = read_json(manifest_path)
    if manifest.get("backup_version") != BACKUP_VERSION:
        raise ValueError(f"unsupported backup version: {manifest.get('backup_version')!r}")
    manifest_session_id = manifest.get("session_id")
    validate_backup_session_id(manifest_session_id)
    if backup_dir.parent.name != manifest_session_id:
        raise ValueError("backup session directory does not match manifest session_id")
    if backup_dir.name != manifest.get("backup_id"):
        raise ValueError("backup directory name does not match manifest backup_id")
    if manifest.get("manifest_digest") != self_digest(manifest, "manifest_digest"):
        raise ValueError("manifest digest mismatch")
    if confirmation != manifest.get("backup_id"):
        raise ValueError("confirmation must exactly equal backup_id")
    if manifest.get("status") != "success":
        raise ValueError("only successful backups can be restored")
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
            sub.add_argument(
                "--recent-compactions",
                type=int,
                default=DEFAULT_RECENT_COMPACTIONS,
                help="preserve from this many latest logical compaction boundaries",
            )
            sub.add_argument(
                "--profile",
                choices=("cache", "balanced", "space", "custom", "target"),
                help="generate one named candidate; omit to compare cache, balanced, and space",
            )
            sub.add_argument("--max-output-bytes", type=int)
            sub.add_argument("--prefix-bytes", type=int)
            sub.add_argument("--suffix-bytes", type=int)
            sub.add_argument("--target-bytes", type=int)
            sub.add_argument(
                "--problem",
                choices=("image_cache", "oversized_output", "overall_size", "context_pressure"),
            )
            sub.add_argument(
                "--retention-priority",
                choices=("recent_content", "visible_messages", "user_images", "structural_fidelity"),
            )
            sub.add_argument("--allowed-strength", choices=("cache", "balanced", "space"))
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
    list_parser.add_argument("--keep", type=int, default=2)
    list_parser.add_argument("--older-than-days", type=int)
    for backups_action in ("prune", "cleanup"):
        cleanup_parser = backups_sub.add_parser(backups_action)
        cleanup_parser.add_argument("--backup-root", type=Path, default=default_backup_root())
        cleanup_parser.add_argument("--session-id", required=True)
        cleanup_parser.add_argument("--keep", type=int, default=2)
        cleanup_parser.add_argument("--older-than-days", type=int)
        cleanup_parser.add_argument("--confirm", help="preview_id returned by a prior backup cleanup preview")
    restore = subparsers.add_parser("restore")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--confirm", required=True)
    restore.add_argument("--backup-root", type=Path, default=default_backup_root())
    restore.add_argument("--target", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            print_json(save_inspection(inspect_file(resolve_target(args.target, args.codex_home)), args.report_dir))
        elif args.command == "plan":
            source = resolve_target(args.target, args.codex_home)
            manual_thresholds = (args.max_output_bytes, args.prefix_bytes, args.suffix_bytes)
            if args.profile is None and any(value is not None for value in manual_thresholds + (args.target_bytes,)):
                raise ValueError("--profile is required when using custom thresholds or --target-bytes")
            if args.profile in {"cache", "balanced", "space"} and any(value is not None for value in manual_thresholds):
                raise ValueError("manual output thresholds require --profile custom")
            if args.profile != "target" and args.target_bytes is not None:
                raise ValueError("--target-bytes requires --profile target")
            stats = inspect_file(source)["stats"]
            intent = build_intent_profile(
                stats,
                problem=args.problem,
                retention_priority=args.retention_priority,
                allowed_strength=args.allowed_strength,
                target_bytes=args.target_bytes,
            )
            if args.profile is None:
                plan = build_plan_set(
                    source,
                    args.report_dir,
                    intent_profile=intent,
                    recent_records=args.recent_records,
                    recent_compactions=args.recent_compactions,
                )
            else:
                plan = build_plan(
                    source,
                    args.report_dir,
                    recent_records=args.recent_records,
                    recent_compactions=args.recent_compactions,
                    max_output_bytes=args.max_output_bytes,
                    prefix_bytes=args.prefix_bytes,
                    suffix_bytes=args.suffix_bytes,
                    profile=args.profile,
                    intent_profile=intent,
                    target_bytes=args.target_bytes,
                )
            print_json(plan)
            return 0 if plan["status"] in {"ready_for_review", "no_change"} else 2
        elif args.command == "audit":
            plan_document = read_json(args.plan)
            audit = audit_plan_set(args.plan) if "plan_set_version" in plan_document else audit_plan(args.plan)
            print_json(audit)
            return 0 if audit["status"] == "pass" else 1
        elif args.command == "apply":
            print_json(apply_plan(args.plan, args.confirm, args.backup_root))
        elif args.command == "backups":
            if args.backups_command == "list":
                print_json(
                    list_backups(
                        args.backup_root,
                        args.session_id,
                        keep=args.keep,
                        older_than_days=args.older_than_days,
                    )
                )
            else:
                print_json(
                    prune_backups(
                        args.backup_root,
                        args.session_id,
                        keep=args.keep,
                        confirm=args.confirm,
                        older_than_days=args.older_than_days,
                    )
                )
        elif args.command == "restore":
            print_json(
                restore_backup(
                    args.backup_dir,
                    args.confirm,
                    args.target,
                    backup_root=args.backup_root,
                )
            )
        return 0
    except (FileNotFoundError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
