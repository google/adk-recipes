# Evaluation Datasets

Behavioral eval cases in Agent Platform `EvaluationDataset` format, run by
`agents-cli eval`. The two suites still under `../evalsets/` are the ones this
runner cannot execute; see [Which runner](#which-runner) below.

## Running

```bash
agents-cli eval run --dataset tests/eval/datasets/smoke-dataset.json \
  --config tests/eval/eval_config.yaml
uv run python scripts/eval_gate.py
```

`eval run` boots `horizon/fast_api_app.py` under uvicorn, replays each case over
HTTP, and grades the traces. It hits real Vertex and costs money per case.

Split the two halves when you want to regrade without paying for inference again:

```bash
agents-cli eval generate --dataset tests/eval/datasets/safety-dataset.json -o traces/
agents-cli eval grade --traces traces/ --config tests/eval/eval_config.yaml
```

Seven cases pre-set session state through a leading `state_delta` event, which
needs a current `agents-cli`; on an older one the state is not applied.

## Which runner

| Suite | Runner | Why |
|---|---|---|
| The 16 datasets here (69 cases) | `agents-cli eval` | Nothing before the graded turn needs to have *run*. Most are single-turn; `compression_quality`'s earlier turn is a pasted compaction banner, which is context, so seeding it is correct. |
| `memory_recall` (8 cases) | `adk eval` | Recall is only meaningful across a session boundary, and an eval case is one session. These cases want rewriting rather than moving. |
| `daily_news_bot_journey` (1 case) | `adk eval` | Its first turn creates a routine as a real row that the second turn edits, and seeded context cannot stand in for the row. Rewriting it as a single turn would let it move. |

## Case Shape

Single-turn cases carry a top-level `prompt`:

```json
{
  "eval_case_id": "ping",
  "prompt": {"role": "user", "parts": [{"text": "Reply with the single word: pong"}]},
  "rubric_groups": {"horizon": {"group_id": "horizon", "rubrics": []}}
}
```

A case that needs seeded session state uses a leading `state_delta` event
instead, since a `prompt` case carries no events:

```json
{
  "eval_case_id": "halt_surfaces_reason",
  "agent_data": {
    "turns": [
      {
        "turn_index": 0,
        "events": [
          {"author": "user", "state_delta": {"halt_reason": "repeated tool failure"}},
          {"author": "user", "content": {"role": "user", "parts": [{"text": "..."}]}}
        ]
      }
    ]
  }
}
```

`expected_tool_uses`, where present, documents the trajectory a case expects. No
metric reads it — it is a note to the next person, carried over from the ADK
evalsets' `intermediate_data.tool_uses`.

## Grading

`../eval_config.yaml` runs one metric, `final_response_quality`, pointed at each
case's own rubrics through `metric_spec_parameters.rubric_group_key`. Every case
also carries the two rubrics the ADK config applied globally (`relevance`,
`helpfulness`), which spell out that preloaded memories, seeded state, and
earlier-turn facts are real context rather than fabrication.

`agents-cli eval grade` reports scores and has no threshold, so
`scripts/eval_gate.py` applies the 0.8 that `adk eval` used to enforce and exits
nonzero below it.

## Adding a Case

1. Copy the nearest case in the suite that fits.
2. Write rubrics that describe the *user-visible* behavior, not the mechanism —
   mechanism belongs in `tests/unit`.
3. Run that one dataset and read the explanations before trusting the score.
