## The selector table: `SELECTOR_TABLE`

`SELECTOR_TABLE` stands for the `table` that `describe_telemetry` returns;
replace it in every recipe below. On this source it is the agents-cli
completions view. It joins the GenAI inference log export with the message
files in GCS, so conversation content is inline and `AI.IF` can judge it. The
session id is the `conversation_id` column.

### Columns worth knowing

`describe_telemetry` lists every column the view has. These are the ones
selectors lean on:

- `timestamp` -- when the inference call was logged, in UTC.
- `trace` -- the trace; one trace is one turn.
- `span_id` -- the inference call.
- `conversation_id` -- the session.
- `message_type` -- `'input'` or `'output'`.
- `role` -- who wrote the message, such as `'user'` or `'assistant'`.
- `content` -- the part's text, inline.
- `part_type` -- such as `'text'`, `'tool_call'` or `'tool_call_response'`.
- `tool_name` -- the tool called, on `tool_call` rows.
- `tool_response` JSON -- what the tool returned.
- `usage_input_tokens`, `usage_output_tokens` -- token counts for the
  inference call, as STRINGs.
- `agent_name` -- the agent. Compare it with `@agent_name`.
- `service_version` -- the deployment revision, from the OpenTelemetry
  `service.version` resource attribute. NULL on log tables created before
  agents-cli exposed it.
- `finish_reasons` -- a JSON array as a string, such as `["stop"]`.

When `describe_telemetry` lists `service_version`, a selector can filter by
deployment revision on it; preview first, since it is NULL on older log tables.
Without that column there is no way to filter by revision.

The view's shape has consequences for every selector:

* **One row per message part.** `usage_*_tokens` and `finish_reasons` repeat on
  every part of the same inference call (`span_id`), so summing tokens over rows
  overcounts. Deduplicate per `span_id`
  first, as recipe 5 does.
* **Token counts are STRINGs.** `usage_input_tokens > '1000'` compares
  lexically, and `'457' > '1000'` is true. Compare numbers only after
  `SAFE_CAST(usage_input_tokens AS INT64)`.
* **`tool_name` is set on `tool_call` rows only**, and NULL on
  `tool_call_response` rows. Match a tool's result by its content, or through
  the `tool_call` row of the same conversation.
* **Input history is kept once.** Each call's input repeats the conversation so
  far; the view keeps each input message from the first call it appears in, and
  drops assistant messages echoed back as input. The assistant's replies are the
  `message_type = 'output'` rows.
* **The window does not reduce the scan.** The view's window functions block
  partition pruning, so every query reads the whole log table and every GCS
  message file whatever the window. Keep `AI.IF` behind cheap predicates -- see
  "Narrow first" in the skill.

### 1. Conversations about a topic, judged by a model

See "Semantic selection with `AI.IF`" in the skill for what this costs and how
to keep it cheap. The regular-expression predicate runs first and the model only
judges what survives it.

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND message_type = 'input'
  AND role = 'user'
  AND part_type = 'text'
  AND conversation_id IS NOT NULL
  AND REGEXP_CONTAINS(LOWER(content), r'cancel|refund|return')
  AND AI.IF(
        ('Does this message ask to cancel an order?', content),
        endpoint => '__AI_MODEL__')
GROUP BY target_id
```

### 2. Conversations that called one named tool

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND part_type = 'tool_call'
  AND tool_name = 'issue_refund'
  AND conversation_id IS NOT NULL
GROUP BY target_id
```

### 3. A tool returned something matching a pattern

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND part_type = 'tool_call_response'
  AND REGEXP_CONTAINS(LOWER(TO_JSON_STRING(tool_response)), r'error|not found')
  AND conversation_id IS NOT NULL
GROUP BY target_id
```

### 4. Turns that took many inference calls

One trace is one turn and one `span_id` is one inference call, so a turn that
made many model calls is a group condition on `trace`.

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND conversation_id IS NOT NULL
GROUP BY target_id, trace
HAVING COUNT(DISTINCT span_id) > 10
```

For a slow turn instead of a busy one, keep the grouping and use
`HAVING TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) > 120`.

### 5. Conversations that used many input tokens

Collapse the rows to one per inference call first, then sum.

```sql
SELECT conversation_id AS target_id
FROM (
  SELECT conversation_id, span_id,
         ANY_VALUE(SAFE_CAST(usage_input_tokens AS INT64)) AS input_tokens
  FROM `SELECTOR_TABLE`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND agent_name = @agent_name
    AND conversation_id IS NOT NULL
  GROUP BY conversation_id, span_id
)
GROUP BY target_id
HAVING SUM(input_tokens) > 20000
```

### 6. The model stopped for a reason other than `stop`

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND conversation_id IS NOT NULL
  AND EXISTS (
        SELECT 1 FROM UNNEST(JSON_VALUE_ARRAY(finish_reasons)) AS reason
        WHERE reason != 'stop')
GROUP BY target_id
```

### 7. A named cohort of conversations

```sql
SELECT conversation_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
  AND conversation_id IN ('sess-4f21', 'sess-90ab')
GROUP BY target_id
```

Take the ids off an insight's occurrences and set a different
`session_review_focus` to re-judge conversations AQuA has already reviewed. The
window still bounds the selection, so point it at when those conversations
happened.
