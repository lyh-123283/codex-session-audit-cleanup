---
name: codex-session-audit-cleanup
description: Audit and conservatively clean one specified Codex conversation history JSONL file on disk. Use when a user names a Codex conversation, session ID, or JSONL path and wants its stored history analyzed, an adaptive multi-candidate cleanup plan shown and independently checked, old tool-output image caches or oversized outputs reduced, backups managed or restored, and changes applied only after explicit confirmation. This skill does not edit the active runtime context, session index, SQLite databases, or writer locks.
---

# Codex Session Audit & Cleanup

Use this skill for one conversation file at a time. It reduces stored JSONL
history on disk while preserving recoverable structure and recent meaning. It
does not shrink a context already loaded into a running request.

## Hard Safety Rules

- Require one concrete target: an absolute `.jsonl` path, a session ID, or a
  conversation name. Resolve names through `session_index.jsonl`; if the match
  is not unique, show the candidates and stop.
- Never touch `session_index.jsonl`, `state_*.sqlite`, `logs_*.sqlite`, WAL/SHM
  files, lock files, or a file that has a writer lock.
- Never edit the active runtime context. The source of truth for this skill is
  the selected JSONL file on disk.
- Use this workflow exactly:
  `inspect -> intent profile -> plan-set or candidate plans -> independent audit
  -> user selects exact candidate -> apply`.
- Do not treat approval of a general strategy as approval of a plan. Require
  the exact current `plan_id` for `apply`.
- Bind every plan and audit to source, candidate, plan, and audit digests. If
  any bound artifact changes, regenerate it and obtain a new confirmation.
- Before replacement, create and hash-check a byte-for-byte original backup.
  Replace through a same-volume temporary file and verify the final hash.
- Keep failed, unknown, corrupt, invalid, and unverifiable backup batches.
  Never delete the last successful recovery point without explicit user choice.

## Workflow

### 1. Resolve and inspect

Run the bundled standard-library helper:

```powershell
python scripts/session_cleanup.py inspect "<path-or-session-id-or-name>" `
  --report-dir "<report-directory>"
```

For a session ID or name, add `--codex-home "<Codex home>"` when the default
`%USERPROFILE%\.codex` is not correct. Report the absolute target path, file
size, record count, time range, logical compaction boundaries, visible-message
count, tool-output/image sizes, malformed lines, call/result pairing, locks,
and the fact that this is disk history rather than active context.

Stop if parsing fails, a writer lock exists, the target is not a regular file,
or the target is ambiguous.

### 2. Build the intent profile and candidates

Use the inspection metrics and the user's request to record:

- `problem`: `image_cache`, `oversized_output`, `overall_size`, or
  `context_pressure`;
- `retention_priority`: `recent_content`, `visible_messages`, `user_images`,
  or `structural_fidelity`;
- `allowed_strength`: `cache`, `balanced`, or `space`;
- optional `target_bytes`;
- explicit assumptions and the inspection evidence supporting them.

Show this intent profile to the user. If the user did not specify a strength,
use `balanced` as the default recommendation, but generate all three named
candidates so the user can compare them. Do not silently turn an inferred
intent into destructive permission.

The default command is:

```powershell
python scripts/session_cleanup.py plan "<same-target>" `
  --report-dir "<report-directory>"
```

With no `--profile` and no manual threshold, this creates a `plan-set` with
independent candidates for `cache`, `balanced`, and `space`. Each candidate
has its own `plan_id`, candidate JSONL, plan JSON, and later audit JSON. The
plan-set itself is an index and comparison document; it is never accepted by
`apply`.

The named profiles are:

| Profile | Transformation |
| --- | --- |
| `cache` | Scrub old tool-output `data:image...` caches only; do not truncate text outputs. |
| `balanced` | Scrub old tool-output image caches and truncate still-large old outputs over 64 KiB, retaining an 8 KiB prefix and 4 KiB suffix. |
| `space` | Scrub old tool-output image caches and truncate still-large old outputs over 16 KiB, retaining a 2 KiB prefix and 1 KiB suffix. |
| `custom` | Use explicit `--max-output-bytes`, `--prefix-bytes`, and `--suffix-bytes`; image-cache scrubbing remains enabled. |
| `target` | Use `--target-bytes`; deterministically choose the least strong threshold that reaches the requested complete-file size. |

`cache`, `balanced`, and `space` are the only profiles in a plan-set.
Generate `custom` or `target` as one explicit candidate with `--profile`.
For `target`, the helper compares the named `space` and `balanced` policies,
then scans 16 KiB through 64 KiB on a 1 KiB grid and selects the largest
threshold whose candidate is at or below the target. A target below the
`space` result is `infeasible`; a source already within the target is
`no_change`. Neither status may be applied.

The default `recent_compactions` is `2`. Preserve records from the two most
recent logical `compacted`/`context_compacted` boundaries onward. If fewer
boundaries exist, use the available boundaries and the recent-record fallback
(default `1000`). The plan and every audit record the selected boundary lines.

### 3. Enforce the transformation boundary

Always preserve:

- every record in the protected recent region;
- all user and assistant visible messages byte-for-byte;
- `session_meta`, compaction records, events, reasoning, token counts, patches,
  tool calls, call IDs, unknown fields, and unknown record types;
- images embedded in user messages.

Candidate changes are limited to old `custom_tool_call_output` and
`function_call_output` records outside the protected region:

- replace structured `input_image` nodes containing `data:image...` cache data
  with a text marker while retaining the surrounding record structure;
- for an old output above the selected threshold, retain a bounded prefix and
  suffix with a middle truncation marker.

Never delete a complete record, delete by age alone, summarize visible
messages, remove old user/assistant content, or clear user-message images. If a
plan proposes anything else, mark it blocked and regenerate under an explicit
reviewed policy.

### 4. Run independent audits

Audit either a single candidate or the plan-set:

```powershell
python scripts/session_cleanup.py audit "<plan-or-plan-set-json>"
```

For a plan-set, the command independently audits every candidate, writes each
candidate audit, and updates the plan-set audit metadata. For one candidate it
writes one audit document. The four named stages are:

1. `schema`: source and candidate are valid UTF-8 JSONL objects;
2. `policy`: only allowed old tool-output lines changed and protected content
   is unchanged;
3. `deterministic_transform`: the candidate is reproducible from the source,
   policy, and intent metadata;
4. `integrity`: record count, session ID, visible-message lines, compaction
   records, tool call/result ID sequences, and image constraints remain valid.

Each stage records input and result digests. `apply` reruns the same stages and
rejects missing, reordered, stale, or tampered results. An audit failure
invalidates the plan; do not edit a candidate to force a pass.

### 5. Show, select, and apply

Show the user the intent profile and, for every candidate:

- profile, exact `plan_id`, status, source SHA-256, and audit status;
- original/candidate sizes, expected savings, changed record count, cleared
  image count, and truncation count;
- every changed line and call ID, or a clearly labeled complete list plus counts;
- protected content categories, backup location, restore command, and residual
  risk;
- target selection details, including `infeasible` or `no_change` reasons.

The user must select one exact candidate and confirm its current `plan_id`.
Never pass the `plan_set_id` to `apply`, and never apply multiple candidates.

```powershell
python scripts/session_cleanup.py apply "<candidate-plan-json>" `
  --confirm "<candidate-plan-id>" `
  --backup-root "<backup-root>"
```

The helper rechecks the source hash and writer lock, verifies the stored audit
against a fresh audit, creates the original backup, performs an atomic
same-volume replace, and verifies the final candidate hash. Report the backup
ID, backup path, final hash, and post-write status. Never silently retry against
a changed source.

Plans with `plan_version: 2` or any older version are stale and must be
regenerated.

### 6. Manage and restore backups

List backups without changing anything:

```powershell
python scripts/session_cleanup.py backups list `
  --backup-root "<backup-root>" --session-id "<session-id>" `
  --older-than-days 2
```

Use `backups cleanup` for the normal preview-and-delete workflow. Keep the
newest two verifiable successful backups by default:

```powershell
python scripts/session_cleanup.py backups cleanup `
  --backup-root "<backup-root>" --session-id "<session-id>" `
  --keep 2 --older-than-days 2
```

The preview includes `preview_id`, `preview_path`, `preview_digest`, exact
deletion paths, reclaimable bytes, retained paths, preservation reasons, and a
complete backup snapshot. Only after the user reviews it, confirm the same
preview:

```powershell
python scripts/session_cleanup.py backups cleanup `
  --backup-root "<backup-root>" --session-id "<session-id>" `
  --keep 2 --older-than-days 2 --confirm "<preview-id>"
```

`backups prune` is a compatibility alias for the same implementation. The
confirmation is bound to preview version, digest, age filter, and the complete
backup snapshot. If anything changes, generate a new preview. Failed, unknown,
invalid, corrupt, symlinked, and invalid-timestamp backups are never automatic
deletion targets. Candidates are rechecked and moved into quarantine before
recursive deletion.

Restore only to the original source path recorded in the manifest:

```powershell
python scripts/session_cleanup.py restore "<backup-directory>" `
  --backup-root "<backup-root>" `
  --confirm "<backup-id>"
```

The helper requires the batch to be directly under
`<backup-root>/<session-id>/<backup-id>`, rejects symlinked batch/session
directories and unsupported manifest versions, and verifies the backup digest,
SHA-256, JSONL parseability, target lock, and post-restore hash. A restore
target cannot be redirected to another conversation.

## Helper Contract

The bundled `scripts/session_cleanup.py` uses only Python's standard library
and exposes `inspect`, `plan`, `audit`, `apply`, `backups list`,
`backups cleanup`, `backups prune`, and `restore`. JSON reports are the durable
interface; console output is for the user. Exit code `0` means success, `1`
means an audit failed, and `2` means an operational or validation error.

Read [references/jsonl-schema.md](references/jsonl-schema.md) when a session
contains unfamiliar records or when deciding whether a new transformation is
safe to add.
