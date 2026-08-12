# Codex Session Audit Cleanup Implementation Plan

> **For agentic workers:** Use the bundled skill instructions and run the test suite before changing behavior.

**Goal:** Build a public, reusable Codex skill for plan-driven cleanup of one on-disk conversation JSONL file.

**Architecture:** Keep interaction policy in `SKILL.md`; keep deterministic JSONL manipulation in one standard-library CLI. Persist plan, audit, and backup manifests as JSON so every write is tied to an immutable source hash and explicit confirmation.

**Tech Stack:** Python 3.10+, standard library, `unittest`, GitHub Actions, GitHub CLI.

## Global Constraints

- Process only one explicitly selected `.jsonl` session file.
- Default to read-only inspection and plan generation.
- Preserve visible messages, protected recent records, tool-call sequences, unknown fields, and user images.
- Never modify session indexes, SQLite/WAL/SHM files, or writer locks.
- Require an independent audit and exact plan-ID confirmation before replacement.
- Preserve failed backups and keep two successful backups by default.

## Tasks

### Task 1: Deterministic session operations

Create `scripts/session_cleanup.py` with subcommands `inspect`, `plan`, `audit`,
`apply`, `backups list`, `backups prune`, and `restore`. Use only standard
library code. Keep source/candidate/plan/audit hashes in JSON reports. Write
same-volume temporary files and use `os.replace` only after backup and checks.

### Task 2: Safety regression tests

Create `tests/test_session_cleanup.py` with synthetic JSONL covering old image
scrubbing, oversized-output truncation, recent-record protection, message
protection, tampered-candidate rejection, exact confirmation, backup creation,
and retention of failed plus newest successful backups.

### Task 3: Skill and schema guidance

Write `SKILL.md` as the user-facing workflow and
`references/jsonl-schema.md` as weak-schema guidance. Explain that the skill
changes disk history only and require visible plan review before apply.

### Task 4: Public repository hygiene

Add a license, `.gitignore`, and GitHub Actions test workflow. Do not include
real session files, reports, backups, credentials, or machine-specific paths.

### Task 5: Verification and release

Run unit tests, skill validation, CLI synthetic smoke tests, and a read-only
inspection of a real session. Initialize a dedicated Git repository in the
skill directory, commit the verified files, create a public GitHub repository,
push the default branch, and verify the remote plus public visibility.
