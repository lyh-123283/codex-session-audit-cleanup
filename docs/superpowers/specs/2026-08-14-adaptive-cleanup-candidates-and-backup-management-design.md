# Adaptive Cleanup Candidates and Backup Management Design

## Goal

Extend the Codex session cleanup skill so it can reason about the user's
cleanup goal, generate multiple auditable candidates with different strengths,
and offer a convenient but separately confirmed way to remove obsolete
backups. The system must remain deterministic at the file-transformation
layer and must never apply a candidate without an exact plan confirmation.

## Scope

The feature applies to one unambiguous Codex session JSONL file at a time. It
adds policy profiles and candidate presentation to the existing
`inspect -> plan -> audit -> confirmation -> apply` workflow. It adds backup
cleanup conveniences without changing the session cleanup confirmation flow.

The feature does not change the active runtime context, `session_index.jsonl`,
SQLite databases, WAL/SHM files, writer locks, or any other session.

## User Intent Profile

The skill derives an explicit intent profile before generating candidates. The
profile is based on user language and inspect facts, never on guessed numeric
goals:

- `problem`: image cache, oversized text output, overall size, or context
  pressure;
- `retention_priority`: recent content, visible messages, user images, or
  structural fidelity;
- `allowed_strength`: explicit user tolerance, otherwise the balanced default;
- `target_bytes`: present only when the user states a target;
- `assumptions`: the decisions made from ambiguous language;
- `evidence`: inspect metrics supporting the recommendation.

The skill presents this profile before the candidates. If a high-risk choice is
ambiguous, it asks one focused question instead of silently choosing it.

## Candidate Policies

Every candidate shares these invariants:

- preserve the requested number of recent logical compaction boundaries,
  defaulting to two;
- preserve all visible user and assistant messages byte-for-byte;
- preserve user-message images, all record types outside eligible old tool
  outputs, unknown fields, tool calls, call IDs, and record count;
- change only old `custom_tool_call_output` and `function_call_output` records;
- retain the JSONL record and call/result structure;
- never delete by age alone, delete a complete record, or generate a model
  summary in place of output content.

The first profiles are:

| Profile | Image cache | Old output threshold | Preview | Intended use |
| --- | --- | --- | --- | --- |
| `cache` | clear | no text truncation | n/a | image-heavy or cache-only requests |
| `balanced` | clear | 64 KiB | 8 KiB prefix + 4 KiB suffix | default recommendation |
| `space` | clear | 16 KiB | 2 KiB prefix + 1 KiB suffix | explicit size pressure |

The existing `plan` command gains a `--profile` selector. The default profile
is `balanced`, preserving the current output transformation policy. The
approved default retention boundary remains two logical compactions. Explicit
profile parameters are the source of truth; a user-supplied threshold is
allowed only through an explicit custom/target mode and is recorded as such in
the plan, never silently mixed with a named profile. Each profile is generated
as its own plan and candidate with its own plan ID, source hash, audit path,
and confirmation requirement. A profile that produces no change is not
presented as a useful candidate.

When the user states a target size, the skill first evaluates the balanced and
space policies and may generate a deterministic custom threshold between them.
The threshold and byte target are recorded in the plan. If the target is not
reachable without touching protected content, the plan is marked infeasible
and explains the remaining protected bytes.

The transformation remains textual and deterministic. It does not ask a model
to summarize tool output, because summaries would be lossy, non-repeatable,
and harder to audit.

## Candidate Workflow

The skill follows this flow:

```text
inspect
  -> intent profile and assumptions
  -> generate cache/balanced/space candidates as applicable
  -> audit each candidate independently
  -> show comparison and complete change lists
  -> user selects one exact plan_id
  -> apply only that plan
```

The comparison includes original and candidate sizes, bytes saved, changed
records, image count, truncation count, protected boundary lines, policy
parameters, source hash, audit status, and residual risk. Plans are never
merged after auditing; selecting one plan invalidates the others only as a
user-interface choice, not by modifying their files.

The audit stages remain schema, policy, deterministic transform, and integrity.
Each stage recomputes the selected profile and boundary metadata from the
source. Apply rejects a stale, changed, cross-boundary, or mismatched plan.

## Backup Management

Session application always creates a byte-for-byte verified original backup.
Backup cleanup is a separate operation and never runs automatically after
apply.

The existing `backups prune` behavior is retained for compatibility and is
exposed through a more user-oriented `backups cleanup` command. Both commands
share the same implementation and preview format:

```text
backups cleanup --backup-root <root> --session-id <id> --keep 2
backups cleanup --backup-root <root> --session-id <id> --keep 2 \
  --older-than-days 30 --confirm <preview_id>
```

Without `--confirm`, the command creates a preview containing:

- the exact backup snapshot and preview ID;
- candidate paths, backup IDs, ages, and byte totals;
- preserved paths and reasons;
- the number of retained valid recovery points;
- the expected reclaimable bytes.

Only successful backups with valid content and manifest integrity can be
deletion candidates. `--keep N` always retains at least one valid successful
backup and defaults to two. `--older-than-days` is an optional additional
filter: a backup is eligible only when its parsed `created_at` is at least that
many days old. Invalid or missing timestamps are preserved rather than
guessed. The age filter can only reduce the candidate set and cannot bypass
the keep rule. Failed, unknown, corrupt, and integrity-failed backups are
preserved.

Confirmation rechecks the complete snapshot and preview digest. Any new,
missing, modified, or reordered backup causes rejection and requires a new
preview. Candidates are moved into a managed quarantine before deletion; a
failure during the move phase restores already moved directories. The
confirmation ID is distinct from a session cleanup `plan_id`.

`backups list` reports integrity, age, size, and deletion eligibility so the
skill can explain why a backup is or is not a candidate. `restore` remains an
explicit operation restricted to the manifest's original source path.

## Safety and Error Handling

- Ambiguous session names, invalid JSONL, writer locks, invalid profiles, and
  infeasible target sizes stop before candidate application.
- Missing profile metadata or an old plan version is rejected and requires a
  new plan.
- A changed source, candidate, plan, audit, or backup snapshot invalidates the
  relevant confirmation.
- No command silently falls back from a requested profile to a stronger one.
- A failed apply retains the failed backup batch and attempts source rollback.
- A failed backup cleanup retains unknown or quarantined data and reports the
  exact recovery state.

The plan schema version is bumped when profile or target-size metadata is
introduced. Old plans without the required policy metadata are rejected and
must be regenerated; `backups prune` remains a compatibility alias for the
new cleanup implementation.

## Verification

Unit tests must cover:

- profile parameter selection and cache-only behavior;
- balanced and space candidates changing only eligible old tool outputs;
- candidate comparison metadata and independent audit binding per profile;
- target-size infeasibility and deterministic threshold selection;
- protected recent compaction boundaries and user images for every profile;
- `backups cleanup` preview, keep-count and age filtering;
- corrupted/failed backup preservation;
- preview rejection after backup-set changes;
- cleanup move failure rollback and path traversal rejection;
- backward compatibility of `backups prune` and existing plan/apply flows.

CLI smoke tests should run the candidate workflow on synthetic JSONL and the
backup workflow on synthetic batches. Documentation must show the natural
language flow and the exact confirmation requirements for both IDs.

## Non-Goals

- deleting complete conversation records;
- modifying user-visible messages or user-message images;
- changing active context or session indexes;
- automatic backup deletion after a session apply;
- model-generated summaries of old tool outputs.
