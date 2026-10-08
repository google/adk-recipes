# ambient-quality-agent: agent instructions

## Naming

- Every function and method name starts with a verb that states what it does. This includes pure helpers.
- Exempt: `@property` members (nouns), dunders, pytest `test_*` functions and fixtures, framework overrides (OpenTelemetry `shutdown`/`force_flush`, ADK callbacks such as `before_agent`).
- Mirrored code shares names:
  - the BigQuery stores and readers under `src/ambient_quality_agent/tools/` ↔ their in-memory counterparts in `src/ambient_quality_agent/standalone/` (`InMemory*`), which implement the same contract;
  - `TrajectoryReader`/`InsightReader` ↔ `tools/mock_aqa_server.py`;
  - `src/ambient_quality_cli/metrics.py` ↔ `src/ambient_quality_agent/tools/metrics/library.py`.

### Verbs

| Verb | Use for |
|---|---|
| `get_` | Fetch one item by key, returning `X \| None`; or several items by keys, returning a dict keyed by id |
| `list_` | Return a collection, optionally filtered or paginated |
| `count_`, `sum_` | Aggregate queries |
| `read_`, `write_`, `store_` | Raw object, file or request-body I/O |
| `load_` | Read and parse into a typed object |
| `fetch_` | Telemetry ingestion paging and remote HTTP only |
| `save_` | Upsert an entity |
| `record_`, `append_` | Append to an event log or a table |
| `insert_` | Raw BigQuery insert |
| `create_` | Create a stored resource, or a default dependency (`_create_default_<thing>`) |
| `build_` | Assemble an in-memory object, including SQL fragments and URLs |
| `render_` | Turn data into display text (markdown, report lines) |
| `format_` | Reshape one value or record for output |
| `parse_`, `decode_` | Text to structure; decode an encoded token |
| `compute_`, `resolve_`, `extract_` | Derive a value; choose among alternatives; pull a field out of an argument |

- Do not use `generate_`, `process_`, `do_`, `make_`, or `handle_` (except UI/event handlers).
- Yes/no functions start with `is_`, `has_`, `can_` or `should_`, or use a verb that reads as a question (`matches_`, `covers`, `defines_`).
- Conversions: `to_<x>` (methods), `from_<x>` (alternate constructors), `<src>_to_<dst>` (free functions). Do not use `as_<x>`.
- CLI command functions are named `<verb>_<noun>_cmd`.
- A name must not shadow a builtin or another name in the same scope.

## Comments and docstrings

These rules apply to comments, docstrings, and user-visible messages (logs, printed output).

- Write plain, professional English that a reader understands on first pass. Use US spelling, unless a file consistently uses another convention.
- Explain **why**, not **what**. Delete a comment that only restates what the code plainly does.
- Be concise, but keep the reasoning a reader needs.
- Describe only the current state. No historical or migration language ("previously", "no longer", "instead of", "now we", "used to").
- No commented-out code.
- Use a numbered list to describe a sequence of steps, so each step maps to the code that performs it.
- Every function docstring documents its arguments and return value in Google style (`Args:`, `Returns:`, `Raises:` where relevant). pytest test functions and fixtures are exempt.
- Every claim must be true of the code. Verify a stated reason before you keep or reword it; never invent one.

## Module diagrams

- Some module docstrings contain an ASCII wiring diagram (the literal block after a line ending in `::`). When you change the wiring a diagram shows (a component, a call between components, or an implementation), update the diagram in the same change.
- Diagrams show stable structure only: components, call direction and method names. No line numbers, parameters or config values.

## Pull request descriptions

- When the `pr-description` subagent is available, write every PR description with it, so all PRs share one format. Use its output as the PR body.
