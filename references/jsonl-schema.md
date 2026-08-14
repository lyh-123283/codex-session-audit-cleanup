# Codex Session JSONL and Report Schema

This is a weak-schema reference for the helper. It is not an official Codex
format specification. Preserve unknown fields and record types. The helper
uses only the fields needed for a checked invariant and copies everything else
unchanged.

## Session JSONL

Each non-empty line is one UTF-8 JSON object. Common top-level fields include
`timestamp`, `type`, and `payload`. Observed top-level record types include:

- `session_meta`
- `event_msg`
- `response_item`
- `world_state`
- `turn_context`
- `compacted`
- `inter_agent_communication_metadata`

Within `response_item.payload`, observed `type` values include `message`,
`custom_tool_call`, `custom_tool_call_output`, `function_call`,
`function_call_output`, `reasoning`, and `token_count`. The exact set varies by
Codex version.

The helper recognizes, but does not normalize, the following relationships:

- tool calls and outputs may pair by `payload.call_id`;
- visible messages may use a `message` payload with `role`, or version-specific
  `user_message`/`agent_message` types;
- a logical compaction boundary is detected from `type: compacted` and/or a
  `payload.type: context_compacted` marker;
- structured images may appear as `{ "type": "input_image", "image_url":
  "data:image/..." }` nodes inside tool output or user-message data.

An empty line, invalid UTF-8, invalid JSON, a non-object top-level value, or a
truncated final line blocks planning. Unknown valid objects are protected from
transformation. JSON parseability is necessary, not sufficient, for safety.

## Protected and Eligible Records

The default protected region begins at the two most recent logical compaction
boundaries. The plan records `requested_logical_compactions`, available and
selected boundaries, `from_line`, and the fallback recent-record count. If
there are not enough boundaries, the available boundaries and recent tail
fallback are used.

The following are always protected:

- records in the protected region;
- user and assistant visible messages, byte-for-byte;
- user-message images;
- compaction records, events, reasoning, token counts, patches, tool calls,
  call IDs, unknown fields, and unknown record types.

Only old `custom_tool_call_output` and `function_call_output` records outside
the protected region can change. Changes are image-cache marker replacement or
bounded text preview truncation. A complete record is never deleted.

## Intent Profile

Every candidate and plan-set records an `intent_profile` object:

```json
{
  "problem": "image_cache",
  "retention_priority": "recent_content",
  "allowed_strength": "balanced",
  "target_bytes": null,
  "assumptions": ["..."],
  "evidence": {
    "record_count": 0,
    "tool_output_bytes": 0,
    "tool_output_count": 0,
    "image_payload_count": 0,
    "user_image_payload_count": 0,
    "logical_compaction_count": 0
  }
}
```

Valid `problem` values are `image_cache`, `oversized_output`,
`overall_size`, and `context_pressure`. Valid `retention_priority` values are
`recent_content`, `visible_messages`, `user_images`, and
`structural_fidelity`. Valid `allowed_strength` values are `cache`,
`balanced`, and `space`. `target_bytes` is either null or an integer of at
least `1`.

## Candidate Plan: Version 3

A single candidate plan is a JSON object with these core fields:

```text
plan_version, plan_id, candidate_kind, requested_profile, status,
created_at, source, candidate, candidate_path, report_path, audit_path,
session_id, source_stats, intent_profile, policy, target, locks,
protected_region, transformation, summary, review_requirements, plan_digest
```

`plan_version` is `3`; plans with version `2` or earlier must be regenerated.
`candidate_kind` is `session_cleanup`. `requested_profile` is one of
`cache`, `balanced`, `space`, `custom`, or `target`.

`source` and `candidate` are fingerprints with `path`, `size`, `mtime_ns`, and
`sha256`. The digest fields are SHA-256 values over the JSON object with its
own digest field omitted. `plan_digest` binds the full plan metadata.

`policy` and `transformation.policy` must be identical. Named profiles contain
`profile`, `scrub_images`, and either null or numeric
`max_output_bytes`/`prefix_bytes`/`suffix_bytes`. A custom policy must retain
image scrubbing and satisfy:

- `max_output_bytes >= 1024`;
- `prefix_bytes >= 1` and `suffix_bytes >= 1`;
- `prefix_bytes + suffix_bytes < max_output_bytes`.

Target plans use `requested_profile: target`, an `intent_profile.target_bytes`,
and a `target` object. The selected transformation policy may be named
`custom`, because target selection chooses a concrete threshold. Target
metadata includes `target_bytes`, lower/upper profile sizes, source size,
`selection_method`, and selected threshold. The status can be:

- `ready_for_review`: a candidate is eligible for audit/apply;
- `no_change`: the source already satisfies the target;
- `infeasible`: the strongest named policy cannot reach the target;
- `blocked`: parsing, lock, or policy validation failed.

`summary` contains `original_bytes`, `candidate_bytes`, `bytes_saved`,
`changed_records`, `image_payloads_cleared`, and `truncated_outputs`.
`transformation.changed_lines` is the human-review list of line numbers,
call IDs, reasons, and byte effects. `protected_region` contains the selected
boundary metadata and preservation rules.

## Plan Set: Version 1

When `plan` is called without `--profile` or manual thresholds, it creates:

```text
plan_set_version, plan_set_id, status, created_at, source,
intent_profile, requested_profiles, candidates, audit_status,
plan_set_path, plan_set_digest
```

`plan_set_version` is `1`. The default `requested_profiles` is
`["cache", "balanced", "space"]`. Each `candidates` entry is an index, not a
second plan, and contains:

```text
plan_id, plan_path, candidate_path, audit_path, source_sha256,
policy, candidate_bytes, bytes_saved, changed_records,
image_payloads_cleared, truncated_outputs, audit_status
```

The plan-set digest binds the complete index. Audit may update its audit
metadata and digest, but `apply` accepts only a single candidate plan. A
plan-set ID is never a valid candidate confirmation.

## Audit Reports

Single-candidate audits use `audit_version: 2` and contain:

```text
audit_version, audit_id, created_at, plan_id, plan_path,
source_sha256, candidate_sha256, plan_digest, status, stages,
checks, errors, audit_digest
```

`stages` contains the four independently checked stages:
`schema`, `policy`, `deterministic_transform`, and `integrity`. Each stage
records an input digest and result digest and must pass.

Plan-set audits use `plan_set_audit_version: 1` and contain:

```text
plan_set_audit_version, audit_id, created_at, plan_set_id,
plan_set_path, source_sha256, status, candidate_audits,
errors, audit_digest, plan_set_digest
```

`candidate_audits` binds each candidate plan ID to its audit path, status, and
audit digest. The audit command writes candidate audit files and updates the
plan-set's audit fields; this is metadata/report writing, not source-session
modification.

## Backup Manifest

Each successful apply creates a directory containing `original.jsonl` and
`manifest.json`. The manifest starts with `backup_version: 1` and includes:

```text
backup_version, backup_id, session_id, status, created_at,
source_path, plan_id, plan_path, audit_path,
original_sha256, original_bytes, final_sha256,
manifest_digest
```

The exact size/hash fields are populated during the apply. Failed batches keep
the same directory and manifest with `status: failed` and an `error` (and, if
needed, `rollback_error`). A manifest digest is computed over the manifest
without `manifest_digest`.

Backup listing adds derived fields such as `path`, `integrity`, `size_bytes`,
`age_days`, `deletion_eligible`, and `deletion_reason`. Successful backups
must verify their manifest digest, original SHA-256, source path validity, and
JSONL parseability to receive `integrity: valid`. Failed/non-success batches
use `not_checked`; unreadable or incomplete batches are `unknown` and remain
protected.

## Cleanup Preview: Version 2

`backups cleanup` and the compatibility alias `backups prune` first write a
preview with:

```text
preview_version, preview_id, created_at, session_id,
keep_successful, older_than_days, evaluation_now,
retained_valid_successful, retained_paths, candidates,
candidate_paths, reclaimable_bytes, preserved_paths,
preserved_reasons, invalid_reasons, snapshot, preview_digest
```

The user-facing preview also reports `status: preview`, `delete_count`, and
`delete_paths`. `snapshot` contains each backup path, ID, status, integrity,
creation time, age, size, manifest SHA-256, and original SHA-256. The
`preview_digest` binds all fields except itself.

Confirmation must supply the exact `preview_id` from a version `2` preview.
The helper recomputes the complete snapshot, keep count, age filter, candidate
paths, and integrity before moving candidates into quarantine. Failed,
unknown, invalid, symlinked, and invalid-timestamp backups are never automatic
deletion targets. `keep >= 1`; `older_than_days`, when present, is `>= 0`.

## Adding a Transformation

Before adding a new candidate rule, document:

1. why the record is safely reconstructible;
2. which visible and structural records remain protected;
3. how call IDs, turn boundaries, images, and unknown fields remain intact;
4. the independent audit that rejects a malformed candidate;
5. restore behavior and the expected failure mode.

Do not use a model-generated summary as a replacement for canonical JSONL.
