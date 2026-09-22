# Context budget

What one turn costs, what stops it growing, and how to check. Horizon pays a
fixed prefix on every model call, not every user turn, and one complex turn is
10 to 20 calls, so a char added here is paid 10 to 20 times.

No current measurements are reproduced below. They go stale, and nothing tests a
number in a markdown file. Every limit named here lives in code or in a test,
and the two commands in [Measuring](#measuring) print today's values.

Related: [`architecture.md`](architecture.md) (where the tiers are assembled),
[`memory.md`](memory.md) (compaction and recall), [`configuration.md`](configuration.md)
(the env vars named here).

## The fixed prefix

Four components:

| Component | Built by |
|---|---|
| `static_instruction` | `conversation/system_prompt.py` → `build_static_instruction()` |
| tool schemas | each tool's docstring |
| `<available_skills>` index | `tools/skill_toolset.py` |
| skills preamble | `tools/skill_toolset.py` |

`tests/unit/test_prompt_budget.py` guards two numbers and no more:
`MAX_TOTAL_PREFIX_CHARS` on the sum, and `MAX_TOOL_DESC_CHARS` on any single
tool description. Both sit about a third above what ships, so they fire when a
whole block or tool set comes back, not when someone edits a sentence.

There is deliberately no per-component ratchet. A cap set just above today's
value tests whether the number changed, which is churn. The prefix reached
70,774 chars with a fully green suite because nobody watched the aggregate, and
the aggregate is what every model call pays for.

## The prefix cache

The prefix is constant, so Vertex serves it from cache after the first call.
`scripts/probe_context_cache.py` runs three turns and prints
`cached_content_token_count` per turn: turn 0 populates the cache and reports
zero, and every later turn should report most of the prompt as cached, reusing
one `cache_name` with `invocations_used` incrementing. If a later turn reports
zero, the cache is broken.

Config is `ContextCacheConfig` in `agent.py`. `min_tokens` there matches
`GeminiContextCacheManager`'s own per-model floor, which is hardcoded for
`gemini-3*`; setting anything below that floor is dead config.

Never put per-turn content in `system_instruction`.
`_generate_cache_fingerprint` hashes the entire string, so one varying line
invalidates the whole prefix on every turn. `PreloadMemoryTool`'s
`<PAST_CONVERSATIONS>` block is the case to watch: it is rebuilt per turn from a
similarity search, so it only stays out of the fingerprint because ADK inserts
it through `LlmRequest._insert_transient_user_content`, which the cache window
excludes. Per-turn steering (iteration count, last error, date, todos) rides the
same region, as trailing `<system-reminder>` content from
`conversation/reminders.py`.

## What bounds growth during a session

Four mechanisms, each covering a case the others miss.

| Mechanism | Where | Bounds |
|---|---|---|
| Source caps | `tools/_output_overflow.py` (`TERMINAL_OUTPUT_LIMIT`, `TERMINAL_OUTPUT_MAX_LINES`), `tools/file_ops.py` (`_MAX_READ_CHARS`, an alias of the former) | One call: `read` and shell output truncate at whichever cap is hit first, spill the remainder to `lha/tool-output/`, and return a pointer. |
| Retroactive pruning | `context/tool_output_pruning.py` | Stale bulk: zeroes old large tool-result bodies before the model reads them, above `DEFAULT_MIN_PART_TOKENS`. Never prunes `PROTECTED_TOOL_SUBSTRINGS` (`subagent`, `clarify`, skills) or any `*_overflow_path`, so expensive or recoverable results survive. Off with `LHA_PRUNE_TOOL_OUTPUTS=0`. |
| Compaction | `context/summarizer.py` → `HorizonSummarizer`; threshold in `context/compaction_threshold.py`; knobs in `agent.py`'s `EventsCompactionConfig` | Total history: fires at a fraction of the active model's input window (`LHA_COMPACTION_WINDOW_FRACTION`), recomputed per turn so `/model` changes it. Horizon adds a REFERENCE-ONLY banner, caps each inlined tool result and call args at `_MAX_HISTORY_ITEM_CHARS`, and tracks files cumulatively so they survive repeated summarization. |
| Preload caps | `memory/preload.py` | Recall: `LHA_PRELOAD_MAX_MEMORIES` and `LHA_PRELOAD_MAX_CHARS`, because ADK's `search_memory` has no `top_k`. |

Source caps and the pruner cover different failures. A cap bounds the worst
single call; the pruner reclaims a long tail of mid-sized results that were each
individually fine. Dropping either one leaves a real session unbounded.

## Keeping the schema surface small

Tool declarations are usually the largest component, so two settings matter:

- `context/declaration_compaction.py` builds the root agent's declarations on
  ADK's legacy path, worth 2,549 chars a turn. The pydantic path emits `title`
  on every parameter, `default: null`, and `anyOf[X, null]`; the legacy path
  expresses the same parameters without them.
  `ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL` selects it process-wide, so the
  module scopes it to one build; child agents (`delegate_builder`, the memory
  forks, `web_research`) stay on the pydantic path.
- `context/schema_normalization.py` runs `inspect.cleandoc` over tool
  descriptions, since the legacy path otherwise ships raw indented docstrings.

A new tool costs its whole description on every call, for as long as it exists,
which is why the per-tool budgets are enforced rather than advisory.

## Measuring

```bash
uv run python scripts/measure_prefix.py         # per-component chars, live agent
uv run python scripts/probe_context_cache.py    # cached tokens per turn (hits Vertex)
uv run pytest tests/unit/test_prompt_budget.py  # the two guards
```

`measure_prefix.py` reads the live agent and measures after normalization, so it
reports what actually ships. Its token figure is a `chars // 4` estimate and
runs high; the probe's `cached_content_token_count` is the measured one.

## Checklist for a change that touches the prompt or a tool

1. Run the budget test.
2. Run `measure_prefix.py` to see where the chars went.
3. If you touched `system_instruction` assembly, run
   `probe_context_cache.py` and confirm turns after the first still report a
   cache hit.
4. Adding a tool means adding it to `tools/names.py`; the registry tests fail
   otherwise.
