# Codex Session Audit & Cleanup

A conservative Codex skill for reducing the size of one on-disk conversation
history JSONL file. It inspects a user-selected session, creates an adaptive
cleanup candidate, runs multiple audit stages, shows the exact plan, and only
applies it after the user confirms the exact plan ID.

This tool changes disk history only. It does not shrink an already-loaded
runtime context, and it does not modify the active conversation, the session
index, SQLite databases, or writer locks.

## Download and Install

This repository is public and contains a self-contained Codex skill. No Python
package installation is required. The simplest installation is to use the
standard Codex GitHub skill installer:

~~~powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
python "$codexHome\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo lyh-123283/codex-session-audit-cleanup `
  --path . `
  --name codex-session-audit-cleanup
~~~

The installer copies the skill into
<code>$codexHome\skills\codex-session-audit-cleanup</code>. Alternatively,
download or clone this public repository and place the repository directory
under <code>$codexHome\skills</code>. Restart or refresh Codex skill discovery
after installation.

## Use in Codex

After installation, call the skill by name and specify one conversation. It
will show the cleanup plan first and wait for confirmation before changing the
file:

~~~text
Use $codex-session-audit-cleanup to inspect the conversation "<conversation-name>".
Show the cleanup plan first; do not modify anything until I confirm the exact plan ID.
~~~

The Python commands below are optional for direct CLI use.

## What It Solves

- An old Codex conversation is too large because of early tool-output image
  caches.
- Older tool outputs contain unusually large text that can be reduced without
  deleting the complete record.
- Recent conversation content and visible user/assistant messages must remain
  intact.
- Old backups need to be inspected, restored, or removed safely.

## Default Policy

The default policy is a conservative hybrid strategy:

- Preserve every record from the latest compaction boundary onward. If there
  is no compaction boundary, preserve the recent tail fallback.
- Preserve all visible user and assistant messages byte-for-byte.
- Preserve <code>session_meta</code>, compaction records, events, reasoning,
  token counts, patches, tool calls, call IDs, unknown records, and unknown
  fields.
- Only consider old tool-output records for transformation:
  - replace real <code>data:image...</code> image caches with a marker while
    retaining the surrounding record structure;
  - for still-large old outputs, replace the output payload with a text preview
    containing a bounded prefix, suffix, and truncation marker in the middle.
- Never delete complete records, delete by age alone, summarize messages,
  remove visible messages, or clear images embedded in user messages.

## Workflow

~~~text
inspect -> plan -> audit -> user reviews and confirms plan_id -> apply
~~~

All stages before <code>apply</code> are read-only. Plans are bound to the
source SHA-256. If the source, candidate, plan, or audit changes, the
confirmation becomes invalid and the plan must be regenerated.

### Audit Stages

The <code>audit</code> command rereads both files and runs four checks:

1. <code>schema</code>: both files are parseable UTF-8 JSONL objects.
2. <code>policy</code>: only declared old tool-output lines changed; protected
   content and visible messages are unchanged.
3. <code>deterministic_transform</code>: the candidate is reproducible from
   the source and the declared parameters.
4. <code>integrity</code>: record count, session ID, tool call/result ID
   sequences, and old tool-output image constraints remain valid.

Each stage reads fresh source/candidate snapshots and records an input digest
and result digest. The <code>apply</code> command runs the same four stage
functions again and rejects the plan if the stored audit differs from that
fresh audit. This is a multi-stage, repeatable review inside one command, not
four separate processes. After the command passes, the skill still presents
every change and its risk to the user and requires an exact
<code>plan_id</code> confirmation.

## Usage

Run the commands below from the repository root. <code>target</code> may be an
absolute JSONL path, a session ID, or a conversation name.

### 1. Inspect the Target

~~~powershell
python scripts/session_cleanup.py inspect "<target>" --report-dir ".\session-cleanup-reports"
~~~

The report includes file size, record count, time range, compaction boundary,
visible-message count, tool-output and image sizes, parse errors, call/result
pairing, writer locks, and the fact that this is disk history rather than
active context.

If the Codex directory is not <code>%USERPROFILE%\.codex</code>, add
<code>--codex-home "&lt;codex-home&gt;"</code>. Do not continue when the target
is ambiguous, malformed, not a regular file, or protected by a writer lock.

### 2. Generate a Plan

~~~powershell
python scripts/session_cleanup.py plan "<target>" --report-dir ".\session-cleanup-reports"
~~~

The command writes:

- <code>plan-&lt;plan-id&gt;.json</code>: the plan, hashes, protected region, and
  change list;
- <code>candidate-&lt;plan-id&gt;.jsonl</code>: a candidate that has not replaced
  the source;
- <code>audit-&lt;plan-id&gt;.json</code>: the path reserved for the later audit
  result; it is written by the <code>audit</code> command.

Review the printed <code>plan_id</code>, changed lines, call IDs, original and
candidate sizes, expected savings, image count, truncation count, and
protected rules. Do not confirm before reviewing the complete plan.

Optional conservative parameters:

~~~powershell
python scripts/session_cleanup.py plan "<target>" --report-dir ".\session-cleanup-reports" --recent-records 40 --max-output-bytes 65536 --prefix-bytes 8192 --suffix-bytes 8192
~~~

All values must be positive, and the prefix plus suffix must fit within the
maximum output size. The defaults are deliberately conservative.

### 3. Audit the Plan

~~~powershell
python scripts/session_cleanup.py audit ".\session-cleanup-reports\plan-<plan-id>.json"
~~~

Continue only when the result has <code>status: pass</code>. Also review every
entry in <code>changed_lines</code>; each entry should have a clear reason,
bounded impact, and reasonable expected savings.

### 4. Confirm and Apply

Only after the user confirms the current complete plan with its exact
<code>plan_id</code>:

~~~powershell
python scripts/session_cleanup.py apply ".\session-cleanup-reports\plan-<plan-id>.json" --confirm "<plan-id>" --backup-root ".\session-cleanup-backups"
~~~

Before replacement, the helper rechecks the source hash and writer lock,
creates and verifies a byte-for-byte original backup, uses a same-volume
temporary file for atomic replacement, and verifies the final candidate hash.
On failure it attempts to restore the original backup.

## Backup Management

Each successful apply creates a batch grouped by session ID and batch ID. Each
batch contains:

- <code>original.jsonl</code>: the original session file;
- <code>manifest.json</code>: status, original path, plan and audit IDs,
  original size, and original/final hashes.

### List Backups

~~~powershell
python scripts/session_cleanup.py backups list --backup-root ".\session-cleanup-backups" --session-id "<session-id>"
~~~

Entries are reported as <code>success</code>, <code>failed</code>, or
<code>unknown</code>, with a separate integrity value of
<code>valid</code>, <code>invalid</code>, or <code>not_checked</code>.
Incomplete, corrupted, or unverifiable backups are retained and are not
automatic prune candidates.

### Preview and Remove Unused Backups

The default is to keep the newest two verifiable successful backups. Generate
a preview first:

~~~powershell
python scripts/session_cleanup.py backups prune --backup-root ".\session-cleanup-backups" --session-id "<session-id>" --keep 2
~~~

The output contains a <code>preview_id</code> and the exact paths proposed for
deletion. After reviewing those paths, confirm the preview:

~~~powershell
python scripts/session_cleanup.py backups prune --backup-root ".\session-cleanup-backups" --session-id "<session-id>" --keep 2 --confirm "<preview-id>"
~~~

The <code>preview_id</code> is bound to the complete backup set at preview
time. If the set changes, confirmation fails and a new preview is required.
Failed, unknown, and integrity-failed backups are never automatic deletion
targets. On confirmation, candidates are rechecked and atomically moved into a
temporary quarantine before recursive deletion. <code>--keep</code> must be at
least <code>1</code>.

### Restore a Backup

~~~powershell
python scripts/session_cleanup.py restore "<backup-directory>" --confirm "<backup-id>"
~~~

Restore can write only to the original source path recorded in the manifest;
it cannot target a different conversation. The backup is checked before
restore, the target is backed up temporarily, and SHA-256 and JSONL
parseability are checked after restore. A failed post-restore check attempts
to roll back the target. A writer lock causes the operation to fail.

## Safety Boundaries and Limitations

- One invocation handles one unambiguous target.
- The helper never modifies <code>session_index.jsonl</code>,
  <code>state_*.sqlite</code>, <code>logs_*.sqlite</code>, WAL/SHM files, or
  lock files.
- It does not change an already-running Codex request context. The session
  normally needs to be reopened or a new request started before a higher-level
  reader observes the disk change.
- Writer-lock detection cannot eliminate every check-to-write concurrency
  window; close the target conversation before applying a plan.
- Restore checks the target lock before the operation, but cannot prevent an
  external writer from appearing after that check.
- Cross-platform Python cannot guarantee persistence across every power-loss
  scenario, so the original backup is always created and hash-verified.

## Development and Verification

The implementation uses only the Python standard library:

~~~powershell
python -m unittest discover -s tests -v
python -m py_compile scripts/session_cleanup.py tests/test_session_cleanup.py
~~~

The tests cover plan protection, candidate tampering detection, exact
confirmation, backup creation and listing, preservation of corrupt backups,
preview binding, pruning, and restore target restrictions.

## Repository Layout

~~~text
SKILL.md                       Codex skill behavior and safety rules
agents/openai.yaml             Skill UI metadata
scripts/session_cleanup.py     Standard-library implementation
references/jsonl-schema.md     Weak schema notes and extension rules
tests/test_session_cleanup.py  Unit tests
~~~

## License

MIT
