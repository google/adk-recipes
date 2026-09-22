# Evaluation Sets

This directory contains evaluation sets for testing agent behavior, in ADK's own
evaluation format. `agents-cli eval run` reads a different `EvalCase` shape
(`prompt` or `agent_data.turns`, see `google.agents.cli.eval.cmd_generate`), so
evalsets here are converted to that shape before running.

## Running Evaluations

```bash
uv sync --extra eval        # one-time: installs google-adk[eval]

uv run adk eval tests/eval/horizon_eval \
  tests/eval/evalsets/<name>.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

Run these against a real Vertex project; they cost money per case.

**Do not use `agents-cli eval run`.** Its inference step rejects any agent
event without `content`, and horizon emits actions-only events from callbacks,
so every case errors before it is graded.

## Evalset Format

Each `.evalset.json` follows the ADK evaluation format:

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
          "user_content": {
            "parts": [{"text": "User message"}]
          },
          "intermediate_data": {
            "tool_uses": [
              {"name": "tool_name", "args": {"param": "value"}}
            ]
          }
        }
      ],
      "session_input": {
        "app_name": "app_name",
        "user_id": "test_user",
        "state": {}
      }
    }
  ]
}
```

## Key Fields

- `eval_cases`: Array of test scenarios
- `conversation`: Sequence of user messages
- `intermediate_data.tool_uses`: Expected tool calls (for trajectory matching)
- `session_input`: Initial session state

## Evaluation Metrics

This repo's `../eval_config.json` declares one metric,
`rubric_based_final_response_quality_v1` (an LLM judge scored against
per-metric rubrics, threshold 0.8). There is no trajectory grader --
`intermediate_data.tool_uses` below is declarative documentation of the
expected trajectory, not something the grader checks.

## Creating Custom Evalsets

1. Copy `basic.evalset.json` as a template
2. Add cases based on your agent's scenarios
3. Include expected tool calls as documentation (see Evaluation Metrics above)
4. Run it with the `adk eval` command above

## Tips

- Start with 3-5 representative cases
- Include both happy path and edge cases
- Test each core capability of your agent
- Add cases when you find bugs in production

See [ADK documentation](https://google.github.io/adk-docs/) for advanced evaluation options.
