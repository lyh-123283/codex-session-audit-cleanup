# Semantic Value Cleanup Design

## Status

Draft for review. This document extends the existing cleanup workflow; it does
not authorize implementation or change the current JSONL transformer.

## Goal

Make the cleanup skill useful for long project conversations by allowing it to
understand what a conversation is doing, identify low-value work blocks, and
propose compact representations of those blocks. The skill must remain
adaptive and conversational at the planning layer while keeping file mutation
strictly auditable and recoverable.

The product is a semantic session curator with a safe file executor. It is not
an age-based deleter, a byte-truncation utility, or a replacement for the
active runtime context.

## Problem

The current implementation chooses changes from record type, protection
boundary, image shape, and byte thresholds. Those rules are useful safety
primitives, but they cannot tell whether an old tool output is a decision,
unique evidence, a failed attempt, a duplicate log, or a regenerable cache.

The previous design also treated model-generated summaries as entirely out of
scope. That makes the user-facing skill feel mechanical. The opposite extreme
is unsafe: writing an unverified model summary into a weak-schema Codex file
can lose facts or break a consumer.

This design separates semantic planning from deterministic execution and adds
explicit gates between them.

## Design Principles

1. The skill decides value; the executor enforces file invariants.
2. "Unknown" means preserve, not delete.
3. A summary is a lossy replacement, not a hidden backup.
4. The cleaned canonical JSONL remains the runtime compatibility baseline;
   the byte-for-byte original backup remains the recovery baseline.
5. The apply step never calls a model. It replays a hash-bound candidate.
6. Savings are secondary to retained meaning and disclosed loss.
7. Every new transformation needs a fixture, an independent audit, and a
   defined restore path.
8. Model-generated claims are review evidence only. The executor trusts only
   fields it can recompute from the source, candidate, and staged artifacts.

## User-Facing Skill Flow

The skill should not begin by selecting `cache`, `balanced`, or `space`.
It should first present an intent reading based on the user's request and
inspection evidence:

- what the user wants to achieve;
- what the conversation appears to contain;
- which phases or work blocks are present;
- what is driving file size;
- what the skill assumes and what remains uncertain.

If a high-impact ambiguity remains, ask one focused question. The question
should use plain language, such as:

- preserve decisions and evidence with minimal loss;
- organize repetitive process while retaining recovery clues;
- maximize space savings with explicitly disclosed information loss.

The skill maps the answer to internal intent fields. Otherwise, it generates
a semantic map and shows several candidates. The user may accept a candidate
or request changes to selected work blocks. Any change creates a new plan and
requires a new exact plan ID.

The main user view is a short narrative and phase timeline. It shows the
number of blocks, the size sources, and the high-impact candidates. Detailed
hashes, sidecar state, and audit stages belong in an expandable safety
section. The comparison is organized by work block, not only by file size.
For every proposed change it shows:

- source lines, timestamps, and `call_id`s;
- semantic role and confidence;
- retained facts and omitted categories;
- original and replacement sizes;
- whether the original remains only in backup;
- the reason the block is considered compressible;
- residual risks and the restore path.

Each candidate also uses four fixed explanations:

- `Retained`: what the reopened conversation can still answer;
- `Omitted`: which raw details are no longer in the cleaned file;
- `Possible impact`: what future investigation may lose;
- `Recovery`: whether the original is available only from a verified backup.

## Work Blocks and Semantic Map

A work block is a bounded unit of project activity. The preferred boundaries
are a visible user turn through its tool results, a phase transition, or a
logical compaction boundary. A block must not cross a protected boundary or
merge unrelated call/output pairs.

The semantic map is an analysis artifact, not a canonical session record:

```json
{
  "semantic_map_version": 1,
  "source_sha256": "...",
  "intent": {
    "goal": "resume_work | reduce_disk | archive | investigate",
    "retention_bias": "anchors | evidence | recent | storage",
    "allowed_loss": "none | low | reviewed"
  },
  "blocks": [
    {
      "block_id": "b-001",
      "source_lines": [1200, 1248],
      "timestamps": ["...", "..."],
      "call_ids": ["..."],
      "phase": "dependency investigation",
      "role": "context",
      "uniqueness": "duplicated | unique | unknown",
      "reconstructability": "exact | partial | none | unknown",
      "later_dependency": "referenced | not_referenced | unknown",
      "confidence": 0.0,
      "proposed_action": "preserve | capsule | extract | cache_scrub | review",
      "evidence": ["..."],
      "reasons": ["..."]
    }
  ]
}
```

The implementation may use a different representation, but it must preserve
these decisions and their evidence in the plan artifacts.

## Content Roles and Actions

The skill classifies content by role before choosing an operation:

| Role | Default action |
| --- | --- |
| `anchor`: requirements, decisions, constraints, final outcomes | `preserve` |
| `evidence`: errors, tests, code, patches, screenshots, external results | `preserve` or `review` |
| `context`: useful process explanation without unique facts | `capsule` or `extract` |
| `transient`: duplicate progress, regenerable cache, repetitive logs | `extract` or `cache_scrub` |
| `unknown` | `preserve` |

The role is not inferred from age or size alone. A large output may be an
important artifact; an old output may contain the only decision rationale.

## Semantic Capsules

A capsule is a compact, source-linked representation of a work block. It may
contain:

- phase and source range;
- directly supported facts;
- decisions and outcomes;
- unresolved questions;
- omitted categories such as repeated progress lines or raw cache bytes;
- confidence and review status;
- a provenance reference to the source and candidate artifacts.

The capsule does not contain a `backup_id` during planning. The backup is
created during apply, after the reviewed source hash is rechecked. The apply
manifest may bind the successful backup ID to an otherwise immutable capsule
bundle; changing the capsule text or its source digest after review is never
allowed.

The full structured capsule is stored in a sidecar artifact. The JSONL, when
in-place mutation is allowed, stores only a rendered text form using an
already-observed `input_text` node. It must not contain a new top-level record,
a new unknown payload type, or an unvalidated `semantic_capsule` object.

Example rendered form:

```text
[session cleanup capsule]
source: lines 1200-1248; call_id=...
phase: dependency investigation
retained facts:
- ...
outcome: ...
omitted: repeated progress output
original: recoverable from the verified backup only
capsule_id: ...
```

The rendered text is not treated as proof that the summary is correct. The
sidecar, source digest, candidate digest, and semantic audit provide that
provenance.

For the first compatibility profile, a text-only output means a list whose
nodes are dictionaries with `type: "input_text"` and string `text` values,
with no other node types, nested structured result objects, or unknown output
fields. A future fixture may expand this allowlist, but it must do so as a
versioned compatibility profile with its own tests. An output that merely
contains one text node alongside other content is not text-only for this
profile.

The replacement target is the exact `payload.output` JSON Pointer of the
eligible tool-output record. The candidate must record whether the original
value was a list or dictionary and must preserve the parent payload and every
field outside that pointer. The executor may not use the existing generic
whole-output truncation routine for a semantic replacement.

All hashes have explicit domains: source and raw-line hashes use the original
UTF-8 JSONL bytes including their original newline; node hashes use the exact
UTF-8 bytes of the selected JSON value serialized with the declared renderer;
sidecar hashes use canonical UTF-8 JSON with sorted keys and no trailing
newline; rendered-text hashes use UTF-8 bytes after fixed `\n` normalization.

## Candidate Types

The plan set may contain these candidate kinds:

- `cache`: deterministic cache handling only;
- `semantic_conservative`: capsules for high-confidence, text-only,
  non-unique context/transient blocks;
- `semantic_balanced`: additionally includes blocks explicitly accepted by
  the user after review;
- `preserve`: analysis only, with no source mutation.

Existing size profiles remain compatibility modes. They must not be described
as semantic value cleanup and must not silently consume semantic decisions.

The user-facing names are intentionally simpler than the internal kinds:

| User choice | Internal candidate |
| --- | --- |
| Clean regenerable caches only | `cache` |
| Organize repetitive process | `semantic_conservative` or reviewed `semantic_balanced` |
| Preview only | `preserve` |

Legacy size modes are advanced options, not peers of the semantic choices.
Each block can be changed from `preserve` to `capsule` or `review` only by
regenerating a candidate and assigning a new `plan_id`.

## Semantic Review Protocol

Semantic candidates require two distinct review passes before structural audit:

1. A planner produces the semantic map and capsule from bounded source blocks.
2. An independent critic checks for unsupported claims, omitted decisions,
   lost failure causes, unique evidence, sensitive data, and incorrect phase
   boundaries.
3. The executor validates source ranges, hashes, record identity, output shape,
   and candidate reproducibility.

Disagreement between planner and critic produces `review` or `blocked`; it does
not produce a lower confidence automatic apply. The exact capsule text is
bound into the candidate before audit and is never regenerated during apply.

## Planner and Executor Contract

The planner and critic produce an immutable candidate bundle. The model-owned
fields are the semantic role, explanation, retained facts, omitted categories,
and review notes. The executor-owned fields are recomputed and must match:

- source file bytes and SHA-256;
- record line, record index, top-level type, payload type, and `call_id`;
- the exact JSON Pointer of the replaced output node;
- the original node bytes and hash;
- the rendered replacement bytes and hash;
- sidecar bytes and hash;
- protected-region and call/output sequence metadata.

The executor must reject a bundle if any model-provided source identity differs
from the recomputed identity. The semantic text is an input to deterministic
candidate rendering, not a reason to relax structural checks.

The independent critic must be a separate review invocation with a distinct
input context and output artifact. A second prompt in the same generated
response is not considered independent.

The candidate bundle passed to the executor has a versioned shape similar to:

```json
{
  "candidate_bundle_version": 1,
  "plan_id": "...",
  "source": {"sha256": "...", "bytes": 0},
  "operations": [
    {
      "block_id": "b-001",
      "line": 1200,
      "record_index": 1199,
      "call_id": "...",
      "json_pointer": "/payload/output",
      "source_node_sha256": "...",
      "replacement_node_sha256": "...",
      "rendered_text": "..."
    }
  ],
  "sidecar": {"path": "...", "sha256": "..."},
  "semantic_review": {"planner": "...", "critic": "..."}
}
```

The exact schema is implementation work, but the boundary is fixed here:
the executor recomputes every identity and hash; planner prose and critic
claims cannot authorize an operation.

## In-Place Compatibility Gate

In-place semantic compression is allowed only when all of the following hold:

- the record is an eligible old tool output outside the protected region;
- `call_id`, record order, and tool call/output sequences remain unchanged;
- the output is a known text-only node shape;
- no code, JSON, patch, configuration, unique error evidence, or ambiguous
  image is being replaced;
- the capsule is represented only with an already-supported text node;
- a real Codex fixture has been reopened and verified after transformation;
- schema, policy, deterministic, semantic, and integrity audits pass;
- a verified original backup exists.

Otherwise the skill may show a sidecar preview, but it must not apply the
semantic replacement to the canonical JSONL.

## Sidecar and Apply Consistency

The sidecar is provenance, not a second recovery source. It must be bound to
the source and candidate hashes. The backup reference is a provenance link;
restore never reconstructs raw output from a capsule.

The apply batch uses this state machine:

```text
new -> backup_verified -> sidecar_staged -> candidate_staged
    -> source_replaced -> verified -> success
                     \-> failed
```

Every state is stored in an atomically replaced manifest and names the
artifacts and hashes that must exist. The required order is:

1. Recheck the reviewed source hash and writer lock.
2. Create and verify the byte-for-byte original backup; obtain immutable
   `backup_id`.
3. Write, flush, and verify the sidecar without changing its reviewed
   capsule bytes. Bind `backup_id` in the manifest, not in the plan digest.
4. Stage and verify the candidate on the same volume.
5. Atomically replace the source, verify its candidate hash, and update the
   manifest to `success`.

The directory and manifest writes must follow the platform's available fsync
rules. Reconciliation is idempotent: it compares the current source hash,
staged candidate hash, sidecar hash, and verified backup hash. It may mark a
fully verified replacement successful, restore the original from backup, or
stop as `needs_manual_recovery`; it must never guess. A sidecar left without a
matching source candidate is `orphaned` and is retained for inspection.

The original byte-for-byte backup remains the only authoritative restore
source. A capsule cannot reconstruct omitted raw output by itself.

Sidecar provenance has an explicit lifecycle: `unbound` during planning,
`active` after a verified source replacement, `stale` when its source or
backup digest no longer matches, and `orphaned` when no matching candidate
source exists. Removing an old backup makes provenance `stale` and must be
reported to the user; it does not silently make the capsule a recovery source.

## Plan and Audit Changes

Adding semantic candidates requires `plan_version: 4`. The new plan must bind:

- `semantic_map` and its source digest;
- capsule sidecar path and digest;
- capsule and rendered-text versions;
- block-level operations and source hashes;
- planner and critic review results;
- compatibility evidence;
- semantic audit status.

Semantic candidate audits use `audit_version: 3` and the ordered stages:

```text
schema
semantic_review
policy
deterministic_transform
integrity
```

Each stage has its own input and result digest. `semantic_review` validates
that the capsule bundle is the reviewed bundle; `deterministic_transform`
re-renders the exact candidate bytes; `integrity` validates record identity,
protected content, call/output sequences, and sidecar binding. Old plans and
audits without these versions and fields are stale and must be regenerated.

The existing plan and audit versions must be rejected as stale when they lack
these fields. The canonical JSONL record invariants remain unchanged.

## Hard Stops

Block the candidate when the target is ambiguous, open, malformed, locked, or
changed; when a protected or unknown record would change; when a capsule fact
has no source evidence; when output shape is unknown; when a boundary is
uncertain; when sidecar and candidate digests disagree; or when compatibility
has not been established. User confirmation can select among permitted
actions, but it cannot override a hard stop or make unknown content safe.

Never compress visible user/assistant messages, user images, reasoning,
compaction records, tool calls, call IDs, unknown records, or unique evidence.

If semantic analysis finds no safe blocks, the skill reports "no safe semantic
compression found" and may still offer a deterministic cache-only candidate.
The user may cancel after the map, request a sidecar-only preview, or choose
to continue with a permitted candidate. A source change, stale plan, or
failed audit is explained in plain language and requires regeneration; it is
not presented as a generic hash error.

The user-facing flow is intentionally four steps:

1. **Understand**: a short natural-language summary of the conversation,
   phases, size sources, and uncertainty.
2. **Group**: a phase timeline with high-confidence blocks first; detailed
   source lines and hashes remain in expandable safety details.
3. **Compare**: at most three candidates, each with retained content, loss,
   impact, savings, and recovery. A block selection change creates a new
   candidate and plan ID.
4. **Confirm**: one sentence restating the final loss, followed by exact
   `plan_id` confirmation.

## Verification Plan

Before implementation is considered complete, tests must cover:

- semantic map binding to source lines and hashes;
- planner/critic disagreement and downgrade behavior;
- capsule rendering into an existing text-node shape;
- rejection of structured, code, JSON, patch, image-evidence, and unknown
  outputs;
- unchanged record order, fields, call IDs, and protected content;
- sidecar/source crash-recovery states;
- manifest state transitions, reconciliation, and idempotent recovery;
- fixture reopen compatibility;
- exact plan confirmation, backup creation, restore, and stale artifact
  rejection.

## Non-Goals

- modifying visible conversation messages;
- claiming to repair an already-loaded runtime context;
- treating a model capsule as lossless;
- automatic deletion of backups;
- using semantic analysis to bypass the existing backup, hash, lock, audit, or
  confirmation requirements.
