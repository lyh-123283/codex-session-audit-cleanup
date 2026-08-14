# Codex Session Audit & Cleanup

This repository contains a self-contained Codex skill for auditing and
carefully reducing one on-disk conversation history JSONL file. It creates an
adaptive intent profile, compares cleanup candidates, independently audits the
selected candidate, and changes the source only after exact confirmation.

The tool changes disk history only. It does not shrink an already-loaded
runtime context and does not modify the active conversation, the session index,
SQLite databases, WAL/SHM files, or writer locks.

## Install

No Python package installation is required. Install from GitHub with the
standard Codex skill installer:

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
python "$codexHome\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo lyh-123283/codex-session-audit-cleanup `
  --path . `
  --name codex-session-audit-cleanup
```

The installer copies the skill to
`$codexHome\skills\codex-session-audit-cleanup`. Refresh Codex skill discovery
after installation.

## Use in Codex

Specify exactly one conversation, session ID, or JSONL path:

```text
Use $codex-session-audit-cleanup to inspect the conversation "<conversation-name>".
Show the intent profile and all cleanup candidates first; do not modify anything until I confirm one exact candidate plan ID.
```

The skill works on the conversation file on disk, not on the current request's
loaded context. Close the target conversation before applying a plan so a
writer cannot change the source between audit and replacement.

## What It Solves

- Early tool-output image caches make one conversation history unusually large.
- Old tool outputs contain large text that can be reduced without deleting the
  complete JSONL record.
- Recent content, visible messages, call IDs, and structural records must stay
  intact.
- Old backups need to be listed, restored, or removed without deleting unsafe
  recovery points.

## Policy and Profiles

The default protected region starts at the two most recent logical compaction
boundaries. A logical boundary normally consists of a `compacted` record and a
following `context_compacted` event. If fewer boundaries exist, the helper uses
the available boundaries and a recent-record fallback (default `1000`).

All user and assistant visible messages, user-message images, non-tool-output
records, tool calls, call IDs, unknown fields, and unknown record types are
preserved. Only old `custom_tool_call_output` and `function_call_output`
records outside the protected region may change.

The named profiles are:

| Profile | Behavior |
| --- | --- |
| `cache` | Replace old tool-output `data:image...` caches with markers; do not truncate text. |
| `balanced` | Also truncate old outputs above 64 KiB, keeping an 8 KiB prefix and 4 KiB suffix. |
| `space` | Also truncate old outputs above 16 KiB, keeping a 2 KiB prefix and 1 KiB suffix. |
| `custom` | Explicit output limits; image-cache scrubbing remains enabled. |
| `target` | Select a deterministic threshold to reach a requested complete-file size. |

The helper never deletes a complete record, deletes by age alone, summarizes
visible messages, removes visible content, or removes images embedded in user
messages.

The skill also records an intent profile:

- `problem`: `image_cache`, `oversized_output`, `overall_size`, or
  `context_pressure`;
- `retention_priority`: `recent_content`, `visible_messages`, `user_images`,
  or `structural_fidelity`;
- `allowed_strength`: `cache`, `balanced`, or `space`;
- optional `target_bytes`, assumptions, and inspection evidence.

When the user does not specify a strength, the default recommendation is
`balanced`, while the default CLI command generates all three named candidates
for comparison.

## Workflow

```text
inspect -> intent profile -> plan-set/candidates -> independent audits -> select one plan_id -> apply
```

All stages before `apply` leave the session source file unchanged, but they do
write reports, candidate JSONL files, audit files, and (for a plan-set) updated
audit metadata.

### 1. Inspect

```powershell
python scripts/session_cleanup.py inspect "<target>" `
  --report-dir ".\session-cleanup-reports"
```

`target` may be an absolute JSONL path, a session ID, or a conversation name.
For a non-default Codex directory, add `--codex-home "<codex-home>"`.

The report includes source size and SHA-256, record count, time range,
compaction boundaries, visible-message count, tool-output/image sizes,
malformed lines, call/result pairing, writer locks, and `safe_to_plan`.
Stop for an ambiguous target, malformed JSONL, a non-regular file, or a writer
lock.

### 2. Generate candidates

With no profile or manual threshold, generate a plan-set:

```powershell
python scripts/session_cleanup.py plan "<target>" `
  --report-dir ".\session-cleanup-reports"
```

This writes:

- `plan-set-<id>.json`, the candidate index and shared intent profile;
- one `plan-<candidate-id>.json` per generated candidate;
- one `candidate-<candidate-id>.jsonl` per candidate;
- an audit path for each candidate, populated by the audit command.

Each candidate has an independent `plan_id`. The `plan_set_id` is only for
grouping and cannot be used as `apply --confirm`.

Generate one named candidate when needed:

```powershell
python scripts/session_cleanup.py plan "<target>" `
  --profile cache --recent-compactions 2 `
  --report-dir ".\session-cleanup-reports"

python scripts/session_cleanup.py plan "<target>" `
  --profile custom --max-output-bytes 32768 `
  --prefix-bytes 4096 --suffix-bytes 2048 `
  --report-dir ".\session-cleanup-reports"

python scripts/session_cleanup.py plan "<target>" `
  --profile target --target-bytes 500000 `
  --report-dir ".\session-cleanup-reports"
```

Manual thresholds require `--profile custom`; `--target-bytes` requires
`--profile target`. `max-output-bytes` must be at least `1024`, both prefix and
suffix must be at least `1`, and their sum must be strictly less than the
maximum. `target-bytes` must be at least `1`.

Target selection first evaluates `space` and `balanced`. For a target between
those results it scans thresholds from 16 KiB through 64 KiB at 1 KiB steps and
chooses the largest threshold whose candidate is at or below the target. A
target below the `space` candidate is reported as `infeasible`; a source already
within the target is `no_change`. These plans cannot be applied.

### 3. Audit

Audit the default plan-set:

```powershell
python scripts/session_cleanup.py audit `
  ".\session-cleanup-reports\plan-set-<id>.json"
```

The plan-set audit independently audits every candidate and writes each
candidate audit. It also updates the plan-set's `audit_status`, candidate audit
summaries, audit ID, and digest. A single candidate can be audited directly by
passing its `plan-<candidate-id>.json` instead.

The four audit stages are:

1. `schema`: source and candidate parse as UTF-8 JSONL objects;
2. `policy`: only allowed old tool-output lines changed;
3. `deterministic_transform`: the candidate is reproducible from source and
   declared parameters;
4. `integrity`: record count, session ID, visible messages, compaction records,
   tool call/result IDs, and image constraints remain valid.

Every stage records input and result digests. `apply` repeats the same checks
and rejects stale or tampered plan, candidate, source, or audit artifacts.

### 4. Select and apply one candidate

Review every candidate's profile, status, exact `plan_id`, source hash, audit
status, changed line/call list, original and candidate sizes, savings, image
count, truncation count, protected rules, and residual risk. Select one
candidate only.

```powershell
python scripts/session_cleanup.py apply `
  ".\session-cleanup-reports\plan-<candidate-id>.json" `
  --confirm "<candidate-id>" `
  --backup-root ".\session-cleanup-backups"
```

`apply` rejects a plan-set, old `plan_version: 2` plans, infeasible/no-change
plans, stale hashes, missing or failed audits, writer locks, and confirmations
that do not exactly equal the selected candidate's `plan_id`.

Before replacement, the helper creates and verifies an original backup, writes
through a same-volume temporary file, atomically replaces the source, and
checks the final candidate SHA-256. A failure leaves a failed manifest and
attempts to restore the original. The active runtime context remains unchanged.

## Backup Management

The default backup root is `~/.codex/session-cleanup-backups` unless
`--backup-root` is provided. Each successful apply creates a batch containing:

- `original.jsonl`, the byte-for-byte source backup;
- `manifest.json`, including source path, session ID, plan/audit IDs, status,
  sizes, hashes, and a manifest digest.

### List

```powershell
python scripts/session_cleanup.py backups list `
  --backup-root ".\session-cleanup-backups" `
  --session-id "<session-id>" --older-than-days 2
```

Entries include `status`, `integrity`, `size_bytes`, `age_days`,
`deletion_eligible`, and `deletion_reason`. Successful backups are eligible
only when their manifest, original hash, and JSONL content verify. Failed,
unknown, incomplete, corrupt, invalid, symlinked, or invalid-timestamp backups
are retained.

### Preview and cleanup

The primary command is `backups cleanup`; `backups prune` remains a compatible
alias:

```powershell
python scripts/session_cleanup.py backups cleanup `
  --backup-root ".\session-cleanup-backups" `
  --session-id "<session-id>" --keep 2 --older-than-days 2
```

Without `--confirm`, the command returns `status: preview`, a `preview_id`,
exact `delete_paths`, candidate sizes and ages, `reclaimable_bytes`, retained
paths, preservation reasons, invalid reasons, and a complete backup snapshot.
Review that output before confirming:

```powershell
python scripts/session_cleanup.py backups cleanup `
  --backup-root ".\session-cleanup-backups" `
  --session-id "<session-id>" --keep 2 --older-than-days 2 `
  --confirm "<preview-id>"
```

`keep` must be at least `1`; `older-than-days` may be `0`. Confirmation is
bound to preview version `2`, preview digest, age filter, and the complete
backup snapshot. If anything changes, create a new preview. Candidates are
rechecked, moved into `.prune-quarantine`, and only then recursively deleted.

### Restore

```powershell
python scripts/session_cleanup.py restore "<backup-directory>" `
  --confirm "<backup-id>"
```

Restore requires the exact manifest `backup_id`, a valid manifest and original
hash, valid JSONL, and no writer lock. It can restore only to the original
`source_path` in the manifest; an alternate conversation is rejected. The
target is hash-checked and parsed after restore, with rollback attempted if
post-restore verification fails.

## Safety Boundaries

- One invocation handles one unambiguous target.
- The helper never modifies `session_index.jsonl`, SQLite, WAL/SHM, lock files,
  or an already-running request context.
- A disk edit is not visible to a loaded context until the conversation is
  reopened or a new request reads the file.
- Writer-lock checks cannot eliminate an external writer appearing after the
  check, so close the target conversation before applying or restoring.
- Cross-platform Python cannot guarantee power-loss persistence, which is why a
  verified original backup is mandatory.

## Development and Verification

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py
python C:\Users\lenovo\.codex\skills\.system\skill-creator\scripts\quick_validate.py .
```

Also run a temporary synthetic-session smoke test through
`inspect -> plan -> audit -> backups list`, and verify that every candidate
audit passes while the source bytes remain unchanged. After publishing, rerun
the installer and verify the installed `SKILL.md` and bundled script match the
published commit.

## Repository Layout

```text
SKILL.md
README.md
agents/openai.yaml
scripts/session_cleanup.py
references/jsonl-schema.md
tests/test_session_cleanup.py
```

## License

MIT
