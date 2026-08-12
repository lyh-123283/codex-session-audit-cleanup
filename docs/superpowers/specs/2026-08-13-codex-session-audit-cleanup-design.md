# Codex Session Audit Cleanup Design

## Goal

Provide a reusable skill that conservatively reduces the on-disk JSONL history
of one user-selected Codex conversation, with a visible adaptive plan,
independent audits, explicit confirmation, verified backups, and convenient
backup pruning/restoration.

## Scope

The skill accepts one absolute JSONL path, session ID, or conversation name.
It does not alter the active runtime context, `session_index.jsonl`, SQLite
databases, lock files, or any other conversation.

## Architecture

`SKILL.md` defines the interaction and safety gates. The standard-library
`scripts/session_cleanup.py` provides deterministic inspection, candidate
generation, independent auditing, atomic application, backup listing/pruning,
and restoration. JSON reports are the handoff between stages, and every plan is
bound to the source SHA-256.

## Adaptive Policy

The plan generator analyzes the selected file and protects the latest
compaction boundary (or recent-tail fallback), all visible messages, all
non-tool-output records, tool calls and IDs, unknown fields, and user images.
It considers only old tool-output records for change: structured image caches
are replaced with a marker, and oversized outputs retain bounded prefix/suffix
text. Complete-record deletion, age-only deletion, message summarization, and
user-image clearing are out of scope for the first version.

## Review Gates

1. Read-only inspection must show a valid, unlocked target.
2. The generated candidate and plan are checked by an independent audit.
3. The model presents changed lines, reasons, size impact, protected content,
   hashes, and residual risk.
4. The user confirms the exact plan ID.
5. Apply rechecks the source hash and lock state, verifies an original backup,
   atomically replaces the source, and verifies the final hash.

Any changed source, candidate, plan, or audit invalidates approval.

## Backup Lifecycle

Each application creates a per-session batch containing `original.jsonl` and a
manifest with hashes, sizes, source path, plan ID, audit path, and status.
`backups prune --keep 2` previews and then removes only older successful
batches. Failed and unknown batches remain available for investigation.

## Verification

The test suite uses synthetic JSONL fixtures to verify plan protection,
independent audit rejection, confirmation enforcement, atomic application,
backup creation, and retention behavior. A live Codex session is used only for
read-only smoke testing; the tool must refuse a live writer lock.
