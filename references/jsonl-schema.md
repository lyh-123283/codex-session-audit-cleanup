# Codex Session JSONL Notes

This reference records the weak schema assumptions used by the helper. It is
not an official Codex format specification. Preserve unknown fields and record
types.

## Observed Layout

Each non-empty line is one UTF-8 JSON object. Common top-level fields include
`timestamp`, `type`, and `payload`. A session commonly begins with a
`session_meta` record. Observed top-level record types include:

- `session_meta`
- `event_msg`
- `response_item`
- `world_state`
- `turn_context`
- `compacted`
- `inter_agent_communication_metadata`

Within `response_item.payload`, observed `type` values include `message`,
`custom_tool_call`, `custom_tool_call_output`, `function_call`,
`function_call_output`, `reasoning`, and `token_count`. The exact set can vary
by Codex version.

## Safety-Relevant Relationships

- Tool calls and tool outputs may be paired by `payload.call_id`.
- User/assistant visible messages may be represented as a `message` payload
  with `role`, or by version-specific message event types.
- Compaction boundaries may be represented by a top-level `compacted` record or
  a `context_compacted` payload.
- Images can occur as structured nodes such as
  `{ "type": "input_image", "image_url": "data:image/..." }` inside a
  tool output or user message.

The helper therefore uses weak recognition: it checks only the fields needed
for a specific invariant and copies every other field unchanged.

## Invalid or Unknown Data

An empty line, invalid UTF-8, invalid JSON, non-object top-level value, or
truncated final line blocks planning. Unknown valid objects do not block a plan
but are protected from transformation. JSON parseability is necessary, not
sufficient, for safe cleanup.

## Adding a New Transformation

Before adding a new candidate rule, document:

1. why the record is safely reconstructible;
2. which visible and structural records are protected;
3. how call IDs, turn boundaries, images, and unknown fields remain intact;
4. the independent audit that rejects a malformed candidate;
5. the restore behavior and expected failure mode.

Do not use a model-generated summary as a replacement for canonical JSONL.
