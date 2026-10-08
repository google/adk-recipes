## The selector table: `SELECTOR_TABLE`

`SELECTOR_TABLE` stands for the `table` that `describe_telemetry` returns;
replace it in every recipe below. On this source it is the ADK BigQuery Agent
Analytics events table. It holds one flat row per event, and the session id is
the `session_id` column.

### Columns worth knowing

`describe_telemetry` lists every column the table has, with the description the
plugin gives each one. The observed agent's table follows its own plugin
version, so a column it lacks is refused as `invalid_sql` by the dry run. The
descriptions leave out three things:

- `agent` -- the agent that wrote the row, which can be a sub-agent.
  `@agent_name` is the root of the agent tree: match it with
  `COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name`,
  as the recipes do.
- `event_type` -- its description cites names the plugin does not write; the
  values it does write are listed below.
- `trace_id` -- when no span was active, it holds an id the plugin generated,
  which may not match any trace in Cloud Trace.

No column holds the deployment revision. A selector can filter by one only if
the agent writes it into `attributes` -- under `custom_tags`, `labels` or
session state, for example -- so preview a filter on it before relying on it.

`event_type` is one of `USER_MESSAGE_RECEIVED`, `LLM_REQUEST`, `LLM_RESPONSE`,
`LLM_ERROR`, `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR`, `TOOL_PAUSED`,
`AGENT_STARTING`, `AGENT_COMPLETED`, `AGENT_ERROR`, `AGENT_RESPONSE`,
`AGENT_TRANSFER`, `AGENT_STATE_CHECKPOINT`, `INVOCATION_STARTING`,
`INVOCATION_COMPLETED`, `INVOCATION_ERROR`, `STATE_DELTA`, `EVENT_COMPACTION`,
`HITL_CREDENTIAL_REQUEST`, `HITL_CONFIRMATION_REQUEST`, `HITL_INPUT_REQUEST`,
`HITL_CREDENTIAL_REQUEST_COMPLETED`, `HITL_CONFIRMATION_REQUEST_COMPLETED`,
`HITL_INPUT_REQUEST_COMPLETED`, `A2A_INTERACTION`.

### Keys inside the JSON columns

Read JSON fields with `JSON_VALUE` (a scalar) or `JSON_QUERY` (an object or
array). These keys are known to appear; the plugin can write others, so treat
the list as a starting point rather than the full set:

- `content.text_summary` -- the user's message, on `USER_MESSAGE_RECEIVED`.
- `content.prompt` and `content.system_prompt` -- on `LLM_REQUEST`.
- `attributes.tools` and `attributes.llm_config` -- on `LLM_REQUEST`. `tools`
  lists either bare tool names or `{name, description, parameters}` objects.
- `content.response` and `content.usage` -- on `LLM_RESPONSE`. `usage` holds
  `prompt`, `completion` and `total` token counts.
- `content.tool`, `content.args`, `content.result` and `content.tool_origin` --
  on the tool events.
- `content.artifacts` and `content.status.state` -- on `A2A_INTERACTION`, where
  `content` is the peer's response.
- `attributes.usage_metadata`, `attributes.model_version`, `attributes.model`,
  `attributes.root_agent_name`, `attributes.session_metadata`,
  `attributes.custom_tags`, `attributes.labels`, `attributes.otel` and
  `attributes.adk`.
- `latency_ms.total_ms`.

The error text of `LLM_ERROR`, `TOOL_ERROR`, `AGENT_ERROR` and
`INVOCATION_ERROR` is on the `error_message` column, which can be empty.
`AGENT_ERROR` and `INVOCATION_ERROR` also carry the traceback in
`content.error_traceback`.

`JSON_VALUE` returns a STRING. Compare a number only after
`SAFE_CAST(... AS INT64)`; compared as strings, `'457' > '1000'` is true.

### 1. Conversations where a tool failed

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND event_type = 'TOOL_ERROR'
  AND session_id IS NOT NULL
GROUP BY session_id
```

### 2. Conversations that used one named tool

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND event_type IN ('TOOL_STARTING', 'TOOL_COMPLETED', 'TOOL_ERROR')
  AND JSON_VALUE(content.tool) = 'issue_refund'
  AND session_id IS NOT NULL
GROUP BY session_id
```

### 3. Conversations that ran long

Elapsed time is the span between the first and last event of the session, so it
is a `HAVING` condition.

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IS NOT NULL
GROUP BY session_id
HAVING TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND) > 120
```

### 4. Conversations that looped on a tool

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IS NOT NULL
GROUP BY session_id
HAVING COUNTIF(
    event_type = 'TOOL_STARTING'
    AND JSON_VALUE(content.tool) = 'search_orders') > 5
```

### 5. Conversations the agent never answered

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IS NOT NULL
GROUP BY session_id
HAVING COUNTIF(event_type = 'USER_MESSAGE_RECEIVED') > 0
   AND COUNTIF(event_type = 'AGENT_RESPONSE') = 0
```

### 6. Escalations to a human that were never completed

Each HITL request type has a matching `_COMPLETED` event, so an unanswered
escalation is a count imbalance over the session.

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IS NOT NULL
GROUP BY session_id
HAVING COUNTIF(event_type IN (
         'HITL_CREDENTIAL_REQUEST',
         'HITL_CONFIRMATION_REQUEST',
         'HITL_INPUT_REQUEST'))
     > COUNTIF(event_type IN (
         'HITL_CREDENTIAL_REQUEST_COMPLETED',
         'HITL_CONFIRMATION_REQUEST_COMPLETED',
         'HITL_INPUT_REQUEST_COMPLETED'))
```

### 7. Agent-to-agent calls that came back empty

An `A2A_INTERACTION` whose `content` carries no artifacts returned nothing
usable to the caller.

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND event_type = 'A2A_INTERACTION'
  AND IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(content.artifacts)), 0) = 0
  AND session_id IS NOT NULL
GROUP BY session_id
```

`JSON_VALUE(content.status.state)` holds the peer's terminal state, whose values
come from the peer rather than from AQuA -- read a few rows in the preview
before filtering on one.

### 8. Conversations about a topic, judged by a model

See "Semantic selection with `AI.IF`" in the skill for what this costs and how
to keep it cheap. The regular-expression predicate runs first and the model only
judges what survives it.

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND event_type = 'USER_MESSAGE_RECEIVED'
  AND session_id IS NOT NULL
  AND REGEXP_CONTAINS(
        LOWER(JSON_VALUE(content.text_summary)), r'cancel|refund|return')
  AND AI.IF(
        ('Does this message ask to cancel an order?',
         JSON_VALUE(content.text_summary)),
        endpoint => '__AI_MODEL__')
GROUP BY session_id
```

### 9. Conversations that used many prompt tokens

Token counts are on the `LLM_RESPONSE` rows. A streamed model call writes one
row per chunk, all sharing the call's `span_id`, so collapse to one row per
call before summing.

```sql
SELECT session_id AS target_id
FROM (
  SELECT session_id, span_id,
         MAX(SAFE_CAST(JSON_VALUE(content.usage.prompt) AS INT64)) AS prompt_tokens
  FROM `SELECTOR_TABLE`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
    AND event_type = 'LLM_RESPONSE'
    AND session_id IS NOT NULL
  GROUP BY session_id, span_id
)
GROUP BY target_id
HAVING SUM(prompt_tokens) > 20000
```

### 10. A named cohort of conversations

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IN ('sess-4f21', 'sess-90ab', 'sess-c113')
GROUP BY session_id
```

### 11. Re-judging conversations AQuA has already reviewed

Take the session ids off an insight's occurrences, set a different
`session_review_focus`, and point the window at when those conversations
happened -- the window still bounds the scan, so a window that does not cover
them matches nothing.

```sql
SELECT session_id AS target_id
FROM `SELECTOR_TABLE`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name
  AND session_id IN UNNEST(['sess-4f21', 'sess-90ab'])
GROUP BY session_id
```
