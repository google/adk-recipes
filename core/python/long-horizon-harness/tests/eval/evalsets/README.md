# Evaluation Sets (replay suites)

Two suites in ADK evalset format, run by `adk eval`. Everything else moved to
`../datasets/` and `agents-cli eval`; see that README for the split.

| Suite | Cases | Why it stays |
|---|---|---|
| `memory_recall` | 8 | Recall is only meaningful across a session boundary, and an eval case is one session. Worth rewriting rather than moving. |
| `daily_news_bot_journey` | 1 | Its first turn creates a routine as a real row that the second turn edits, and seeded context cannot stand in for the row. |

## Running

```bash
uv sync --extra eval        # one-time: installs google-adk[eval]

uv run adk eval tests/eval/horizon_eval \
  tests/eval/evalsets/<name>.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

Runs against a real Vertex project; costs money per case. `../eval_config.json`
declares one grader, `rubric_based_final_response_quality_v1` at threshold 0.8,
with the two global rubrics (`relevance`, `helpfulness`) applied to every case.

## Format

```json
{
  "eval_set_id": "unique_id",
  "name": "Human-readable name",
  "description": "What this evalset tests",
  "eval_cases": [
    {
      "eval_id": "case_id",
      "conversation": [
        {
          "user_content": {"parts": [{"text": "User message"}]},
          "rubrics": [
            {
              "rubric_id": "what_this_checks",
              "rubric_content": {"text_property": "The response ..."}
            }
          ]
        }
      ],
      "session_input": {"app_name": "app", "user_id": "test_user", "state": {}}
    }
  ]
}
```

Every turn after the first is replayed through the agent, which is the whole
reason these two suites live here. `intermediate_data.tool_uses` documents the
expected trajectory; no grader reads it (there is no trajectory grader).
