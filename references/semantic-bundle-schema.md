# Semantic Cleanup Bundle

This document defines the JSON bundle accepted by the semantic cleanup
executor. The bundle is an analysis artifact. It is never appended to the
canonical session JSONL as a new record type.

## Trust Boundary

The skill or planner owns semantic fields: intent, phase, role, retained
facts, omitted categories, reasons, confidence, and review prose. These fields
are advisory evidence and do not authorize a file mutation by themselves.

The executor owns and recomputes source identity, line and record indexes,
call IDs, JSON pointers, node hashes, protected-region checks, candidate bytes,
sidecar hashes, and structural invariants. A mismatch is a hard validation
failure. The executor does not mutate the input records while validating a
bundle.

## Accepted Shape

The first compatibility profile requires the following top-level object:

```json
{
  "semantic_map_version": 1,
  "source": {
    "sha256": "64 lowercase hexadecimal characters",
    "bytes": 12345
  },
  "blocks": [
    {
      "block_id": "b-001",
      "source_lines": [1200, 1248],
      "role": "context"
    }
  ],
  "operations": [
    {
      "block_id": "b-001",
      "line": 1234,
      "record_index": 1233,
      "call_id": "call-...",
      "json_pointer": "/payload/output",
      "source_node_sha256": "64 lowercase hexadecimal characters",
      "rendered_text": "[session cleanup capsule]\n..."
    }
  ],
  "sidecar": {
    "capsule_id": "capsule-...",
    "rendered_text": "[session cleanup capsule]\n..."
  }
}
```

`semantic_map_version` is currently `1`. `source.bytes` is the exact byte
length of the complete source JSONL, and `source.sha256` is SHA-256 over those
bytes, including each original line ending. `blocks` may contain additional
semantic fields such as `timestamps`, `phase`, `uniqueness`,
`reconstructability`, `later_dependency`, `confidence`, `proposed_action`,
`evidence`, and `reasons`. Unknown semantic fields are retained in reports but
are not trusted as structural authorization.

`semantic_map` may also be included as an advisory object. When it contains a
`source_sha256`, that digest must equal `source.sha256`. A future bundle
version may make the wrapper mandatory; version 1 keeps the direct fields
above for compatibility with the skill's first executor integration.

## Blocks

Each block has a unique non-empty `block_id` and an inclusive two-integer
`source_lines` range. The range is one-based and must fit within the parsed
source. A block must not cross the protected boundary. Operations must name an
existing block and target a line inside that block. Duplicate block IDs and
duplicate operation target lines are rejected.

The recommended semantic fields are:

| Field | Meaning |
| --- | --- |
| `role` | `anchor`, `evidence`, `context`, `transient`, or `unknown` |
| `proposed_action` | `preserve`, `capsule`, `extract`, `cache_scrub`, or `review` |
| `uniqueness` | `duplicated`, `unique`, or `unknown` |
| `reconstructability` | `exact`, `partial`, `none`, or `unknown` |
| `later_dependency` | `referenced`, `not_referenced`, or `unknown` |
| `confidence` | Planner confidence in the range 0.0 to 1.0 |

`unknown`, `unique`, protected, visible, or ambiguous material must be
preserved or blocked. It cannot be made safe by a higher confidence value.

## Operations

The first in-place profile accepts only an old `custom_tool_call_output` or
`function_call_output` record outside the protected region. Its
`payload.output` must be a non-empty list in which every node has exactly the
keys `type` and `text`, with `type: "input_text"` and a string `text` value.
Mixed text/image lists, dictionaries, nested structured output, unknown node
fields, code, JSON, patches, configuration, unique evidence, and ambiguous
images are not eligible.

`line` is one-based and `record_index` is zero-based (`record_index = line -
1`). `call_id` must match the source record. The only supported pointer in
this profile is exactly `/payload/output`; the pointer must resolve to the
selected node before the node-shape check is applied. The executor rejects
visible user/assistant records, protected lines, missing call IDs, changed
record identity, duplicate targets, and unknown pointers.

`rendered_text` is the already-reviewed capsule text. It is normalized to
`\n` line endings before rendering. The canonical JSONL replacement is only
the existing node shape:

```json
[{"type": "input_text", "text": "<rendered_text>"}]
```

No `semantic_capsule` record or new payload type is written into the session.
The full structured capsule belongs in the sidecar.

Optional `source_line_sha256` binds the complete original UTF-8 line,
including its newline. Optional `replacement_node_sha256` binds the rendered
replacement node. If supplied, both are recomputed and must match.

## Sidecar

The sidecar is provenance, not a recovery source. Version 1 requires a
non-empty `capsule_id`; a single-operation bundle may include `rendered_text`
which must equal the operation text. Later stages add the staged sidecar path,
canonical byte length, digest, source digest, candidate digest, capsule
version, and lifecycle state (`unbound`, `active`, `stale`, or `orphaned`).

The original JSONL remains the only authoritative recovery source. A sidecar
must never be used to reconstruct omitted raw output.

## Review Metadata

Bundles may carry semantic review metadata such as:

```json
{
  "semantic_review": {
    "planner": {"artifact": "...", "status": "pass"},
    "critic": {"artifact": "...", "status": "pass"},
    "independent": true,
    "disagreements": []
  }
}
```

The planner and critic must be separate review invocations with distinct
artifacts and input context. Unsupported claims, omitted decisions, unique
evidence, uncertain boundaries, or planner/critic disagreement produce
`review` or `blocked`; they do not authorize an automatic downgrade.

## Hash Domains

The executor uses explicit serialization domains:

| Artifact | Bytes hashed |
| --- | --- |
| Source and raw line | Original UTF-8 JSONL bytes, including original newline |
| Selected JSON node | Compact UTF-8 JSON with `ensure_ascii=false`, no spaces, preserving parsed key order, no newline |
| Sidecar and metadata | Compact UTF-8 JSON with sorted keys, `ensure_ascii=false`, no newline |
| Rendered text | UTF-8 text after fixed CRLF/CR to LF normalization |

All digests are SHA-256 lowercase hexadecimal strings. Source, selected node,
and sidecar digests are independent bindings; one digest must not be reused as
proof for another domain.

## Hard Stops

Validation fails closed for malformed JSONL, missing source identity, source
changes, invalid ranges, duplicate targets, unknown pointers, protected or
visible records, missing call IDs, structured or unknown output shapes,
mismatched node/raw-line hashes, missing sidecar data, or unsupported semantic
map versions. User confirmation cannot override these failures.
