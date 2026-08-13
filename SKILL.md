---
name: codex-session-audit-cleanup
description: Audit and conservatively clean one specified Codex conversation history JSONL file on disk. Use when a user names a Codex conversation, session ID, or JSONL path and wants its stored history analyzed, an adaptive cleanup plan shown and independently checked, old tool-output image caches or oversized outputs reduced, backups managed or restored, and changes applied only after explicit confirmation. This skill does not edit the active runtime context, session index, SQLite databases, or writer locks.
---

# Codex Session Audit & Cleanup

Use this skill for one conversation file at a time. The goal is to reduce the
stored JSONL history on disk while preserving the conversation's recoverable
structure and recent meaning. Do not claim that changing the file will shrink a
context already loaded into a running request.

## Hard Safety Rules

- Require a concrete target: an absolute `.jsonl` path, a session ID, or a
  conversation name. Resolve names through `session_index.jsonl`; if the match
  is not unique, show the candidates and stop.
- Never touch `session_index.jsonl`, `state_*.sqlite`, `logs_*.sqlite`, WAL/SHM
  files, lock files, or a file that has a writer lock.
- Treat every run as `inspect -> plan -> audit -> user confirmation -> apply`.
  The first three stages are read-only. Do not apply a plan merely because a
  user previously approved a general strategy.
- Bind a plan and audit to the source SHA-256. If the source, candidate, plan,
  or audit changes, discard the approval and regenerate.
- Before replacement, create and hash-check a byte-for-byte original backup.
  Write a same-volume temporary file and replace atomically. On any failure,
  restore the original backup and report the failure.
- Keep failed or unknown backup batches. Never delete the last successful
  recovery point without explicit user choice.

## Workflow

### 1. Resolve and inspect

Run the standard-library helper from this skill directory:

```powershell
python scripts/session_cleanup.py inspect "<path-or-session-id-or-name>" `
  --report-dir "<report-directory>"
```

For a session ID or name, add `--codex-home "<Codex home>"` when the default
`%USERPROFILE%\.codex` is not correct. Report the absolute target path, file
size, record count, time range, latest compaction line, visible-message count,
tool-output/image sizes, malformed lines, call/result pairing, locks, and the
fact that this is disk history rather than active context.

Do not proceed if JSONL parsing fails, a writer lock exists, the target is not a
regular file, or the target is ambiguous.

### 2. Generate an adaptive plan

```powershell
python scripts/session_cleanup.py plan "<same-target>" `
  --report-dir "<report-directory>"
```

The command writes a plan JSON, an unreviewed candidate JSONL, and prints the
plan. Explain the exact changed line numbers, call IDs, original/candidate
sizes, expected savings, image count, truncation count, and risk.

Use `--recent-compactions 2` when the user requests the latest two logical
compaction boundaries. Codex normally writes `compacted` and then
`context_compacted` for one event. The plan records the selected boundary
lines, and each audit recomputes them from the source file.

The default plan is deliberately conservative:

**Always preserve**

- Every record from the latest logical `compacted`/`context_compacted` boundary
  onward by default. Use `--recent-compactions N` to preserve from the N most
  recent logical boundaries. If there is no compaction boundary, preserve the
  recent tail fallback.
- All user and assistant visible messages, byte-for-byte.
- `session_meta`, compaction records, turn/event records, reasoning, token
  counts, patches, tool calls, call IDs, and any record not confidently known
  to be an old tool output.
- Images inside user messages.
- Unknown fields and record types.

**Candidate changes**

- Only old `custom_tool_call_output` or `function_call_output` records.
- Replace structured `input_image` nodes whose URL is a `data:image...` cache
  with a text marker while retaining the surrounding record structure.
- For an old tool output still over 64 KiB, replace its output payload with a
  text preview containing a bounded prefix, suffix, and
  `[older tool output middle truncated]` marker.
- Never delete a complete record, delete by age alone, summarize messages,
  remove old user/assistant content, or clear user-message images by default.

If the plan would alter anything outside these rules, mark it blocked and do
not invent a workaround. A future policy can be added only as an explicit,
separately reviewed strategy.

### 3. Run multi-stage audits

```powershell
python scripts/session_cleanup.py audit "<plan-json>"
```

Treat the audit as a fresh review, not as confirmation of the plan generator.
It runs four named stages. Each stage rereads the source/candidate snapshots,
records an input digest and result digest, and must pass independently:

- source and candidate hashes still match the plan;
- both files parse line-by-line as UTF-8 JSONL objects;
- record count, session ID, visible-message lines, compaction records, tool
  calls, and call/result ID sequences are unchanged;
- every changed line is an allowed old tool-output line;
- the protected region is byte-for-byte unchanged;
- old tool-output image nodes are gone and no unexpected image node was added.

The `apply` command runs the same four stage functions again and compares their
results with the stored audit. A missing, reordered, tampered, or stale stage
invalidates the plan. The stages are repeatable checks in one command, not
separate processes.

Then perform a human-facing risk review of the printed `changed_lines` list:
every line must have a clear reason, bounded impact, and expected savings. An
audit failure invalidates the plan. Do not edit the candidate to make an audit
pass; regenerate the plan instead.

### 4. Show the plan and wait for confirmation

Show the user:

- target path, session ID, source SHA-256, and plan ID;
- original size, candidate size, expected savings, and changed record count;
- every changed line/call ID and reason, or a clearly labeled full list plus
  counts when the list is long;
- exact protected content categories;
- backup location and restore command;
- the fact that active context is unchanged;
- any residual risk.

Ask for explicit confirmation containing the plan ID. Silence, a vague
approval, or an approval of an earlier version is insufficient unless the exact
plan ID is supplied.

### 5. Apply only the reviewed plan

```powershell
python scripts/session_cleanup.py apply "<plan-json>" `
  --confirm "<plan-id>" `
  --backup-root "<backup-root>"
```

The helper rechecks the source hash and lock state, verifies the independent
audit, creates the original backup, performs an atomic same-volume replace,
and verifies the final candidate hash. Report the backup ID, backup path, final
hash, and post-write status. Never silently retry against a changed source.

### 6. Manage and restore backups

List backups without changing anything. The output includes an integrity value
for successful batches; only `integrity: valid` batches can become prune
candidates:

```powershell
python scripts/session_cleanup.py backups list `
  --backup-root "<backup-root>" --session-id "<session-id>"
```

Preview removal of successful backups while keeping the newest two:

```powershell
python scripts/session_cleanup.py backups prune `
  --backup-root "<backup-root>" --session-id "<session-id>" --keep 2
```

Only after reviewing that preview, run the command again with
`--confirm "<preview-id>"`. The preview ID binds confirmation to the exact
backup set the user reviewed; if the set changes, create a new preview. Failed
and unknown batches are retained and are never included in automatic prune
candidates.

Restore a successful batch explicitly:

```powershell
python scripts/session_cleanup.py restore "<backup-directory>" `
  --confirm "<backup-id>"
```

Verify the restored SHA-256 and parseability before reporting success.

## Helper Contract

The single bundled `scripts/session_cleanup.py` uses only Python's standard
library and exposes these subcommands: `inspect`, `plan`, `audit`, `apply`,
`backups list`, `backups prune`, and `restore`. JSON reports are the durable
interface; console output is for the user. Exit code `0` means success, `1`
means an audit failed, and `2` means an operational or validation error.

Read [references/jsonl-schema.md](references/jsonl-schema.md) when a session
contains unfamiliar records or when deciding whether a new transformation is
safe to add.
