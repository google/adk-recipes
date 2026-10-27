## The selector table: `SELECTOR_TABLE`

`SELECTOR_TABLE` stands for the `table` that `describe_telemetry` returns;
replace it in every recipe below. On this source it is the Cloud Trace linked
dataset. It holds one row per span. The session id is the
`gen_ai.conversation.id` span attribute, not a column.

### Columns worth knowing

`describe_telemetry` lists every column the table has, nested fields included.
These are the ones selectors lean on:

- `trace_id` -- the trace; one trace is one turn.
- `span_id` -- the span.
- `name` -- the span name, such as `call_llm` or `execute_tool <name>`.
- `start_time`, `end_time` -- when the span started and ended, in UTC.
- `attributes` JSON -- the span attributes; the keys are described below.
- `events` -- events recorded on the span, each with a `name` and
  `attributes`.
- `status.code` -- the span outcome: 0 unset, 1 ok, 2 error.
- `resource.attributes` JSON -- attributes of the process that emitted the
  span.

The column set is fixed; the keys inside `attributes` are not. They depend on
what the observed agent's instrumentation emits, so check a key with a preview
before relying on it.

Read attributes with `JSON_VALUE(attributes, '$."<key>"')`. The keys that
matter: `gen_ai.conversation.id` (the session), `gen_ai.agent.name`,
`gen_ai.tool.name`, `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.system_instructions`, `gen_ai.tool.definitions`. The deployment revision
is a resource attribute: `JSON_VALUE(resource.attributes, '$."service.version"')`.

**Execution spans carry no session id.** `call_llm` and `execute_tool <name>`
spans omit `gen_ai.conversation.id`, so a condition on a tool, an error status
or a span name cannot project the session directly. Match those spans, keep
their `trace_id`, and join back to the spans that do carry the session id. Both
sides read the same table, so the join is allowed.

An error shows up two ways: `status.code = 2`, and an `exception` entry in the
span's `events` array.

A long payload may be offloaded to GCS under the same attribute key with a
`_ref` suffix, holding a `gs://` URI instead of the text. Content predicates
match only what is inline.

### 1. Conversations containing a failed span

```sql
SELECT JSON_VALUE(s.attributes, '$."gen_ai.conversation.id"') AS target_id
FROM `SELECTOR_TABLE` AS s
JOIN (
  SELECT DISTINCT trace_id
  FROM `SELECTOR_TABLE`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND status.code = 2
) AS failed
USING (trace_id)
WHERE s.start_time BETWEEN @window_start AND @window_end
  AND JSON_VALUE(s.attributes, '$."gen_ai.agent.name"') = @agent_name
  AND JSON_VALUE(s.attributes, '$."gen_ai.conversation.id"') IS NOT NULL
GROUP BY target_id
```

Swap the inner `WHERE` to select by tool -- `JSON_VALUE(attributes,
'$."gen_ai.tool.name"') = 'issue_refund'`, or `name = 'execute_tool
issue_refund'` -- or by elapsed span time,
`TIMESTAMP_DIFF(end_time, start_time, SECOND) > 30`.

### 2. Conversations from one deployment revision

```sql
SELECT JSON_VALUE(attributes, '$."gen_ai.conversation.id"') AS target_id
FROM `SELECTOR_TABLE`
WHERE start_time BETWEEN @window_start AND @window_end
  AND JSON_VALUE(attributes, '$."gen_ai.agent.name"') = @agent_name
  AND JSON_VALUE(resource.attributes, '$."service.version"') = '12'
  AND JSON_VALUE(attributes, '$."gen_ai.conversation.id"') IS NOT NULL
GROUP BY target_id
```

The `AI.IF` pattern in the skill's "Semantic selection with `AI.IF`" works here
too, over `JSON_VALUE(attributes, '$."gen_ai.input.messages"')`, on spans that
carry the messages inline.
