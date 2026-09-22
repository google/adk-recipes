# Memory layer

**What this doc is.** A code-grounded walkthrough of Long Horizon's memory layer —
the thin custom code wrapped around Memory Bank — for engineers
debugging memory behavior or lifting the self-improvement loop into another agent.

## In this doc

- **§1 The managed core** — the ADK-owned `memory_service` + how `PreloadMemoryTool` surfaces hits each turn.
- **§2 Writing to memory** — the `memory` tool, the throttled `auto_capture` flush, content-safety scanning.
- **§3 The self-improvement loop** — the fire-and-forget review + flush forks via `SiblingAgentPlugin`.
- **§4 The nightly "dream" pass** — `memories.generate` consolidating the Structured Profile + general memories.
- **§5 Pre-compaction flush wiring** — `HorizonSummarizer` rescuing facts before old events are summarized away.
- **§6 Skills vs memory** — the skill curator/telemetry tenants that ride Memory Bank but aren't user facts.
- **§7 Wiring map** — the lift-it table of ADK hook → file.
- **§8 Honesty callouts** — what's gated, best-effort, or Vertex-only.
- **Troubleshooting** — debug-by-symptom: profile empty, dedupe, forks not firing.
- **Where to go next** — sibling subsystem docs.

How Horizon remembers across sessions: a thin custom layer over Vertex AI
Memory Bank plus a fire-and-forget self-improvement loop. There is **no
custom vector DB or SQLite for memory** — Memory Bank is the only durable
cross-session store.

Verified against `horizon/memory/`, `horizon/agent.py` (callback wiring),
`horizon/fast_api_app.py` (service resolution),
`horizon/infrastructure/memory_config.py`, `horizon/scheduler/dream_review_endpoint.py`,
and `horizon/context/summarizer.py`.

> **Where the custom code is.** The store and its consolidation are
> managed by ADK + Vertex; Horizon owns (a) the **write surface** (the
> `memory` tool + a throttled after-turn flush), (b) a **per-turn
> background judge fork** that decides what's worth saving without blocking
> the user's response, (c) a **pre-compaction flush** so facts aren't lost
> when old turns are summarized away, and (d) the **nightly dream pass**
> that consolidates a Structured Profile + dedupes general memories
> server-side. Items (b)–(d) are the interesting part to lift.

## 1. The managed core

`memory_service` is set on the ADK `Runner`, not constructed inside the
memory layer. `horizon/fast_api_app.py:build_runner` resolves it from the
environment:

- **Tests / local:** `InMemoryMemoryService` (from ADK's
  `create_memory_service_from_options`).
- **Deploy:** `VertexAiMemoryBankService` (an Agent Engine resource with
  Memory Bank enabled).

Every memory path reads the service off the runtime
(`tool_context._invocation_context.memory_service`,
`callback_context._invocation_context.memory_service`,
`runner.memory_service`) — so the whole layer no-ops cleanly when no
service is configured.

**How memory is surfaced each turn:** via ADK's built-in
**`PreloadMemoryTool()`**, registered in the root agent's `tools` list
(`horizon/agent.py`). It queries `memory_service.search_memory()` and injects
the hits into context. There is no custom `before_model` memory-search
callback — `PreloadMemoryTool` is the whole *search*-based prefetch.

It is not the only memory-derived injection, though. The structured user
profile is a second, separate path: `session_start.py` loads it once per
session into `state["user_profile"]`, and `build_volatile_reminder`
(`horizon/conversation/reminders.py`) renders it into the prompt on **every**
turn via `render_user_profile`. So: `PreloadMemoryTool` = per-turn similarity
search; user profile = once-per-session load, re-rendered each turn.

## 2. Writing to memory

**`memory(content, scope)` tool** (`horizon/memory/add_memory_tool.py`) —
the LLM-callable write surface, also in the root agent's `tools`. Two
scopes: `"user"` (who the user is — name, role, preferences;
`USER_CHAR_LIMIT = 1375`) and `"agent"` (the agent's own notes — env,
conventions, lessons; `MEMORY_CHAR_LIMIT = 2200`). The text is prefixed
with a `[user] ` / `[agent] ` marker, deduped against an exact-text search
(`_writer.entry_exists`), then appended as an `Event` carrying a
`lha_memory_scope` tag (`_writer.write_memory_event`). Content is scanned
first by `_content_safety.scan_memory_content` (rejects invisible-unicode
and prompt-injection / exfil patterns — memory is replayed into future
turns, so it's an untrusted-input boundary).

**`auto_capture_callback`** (`horizon/memory/auto_capture.py`, registered as an
`after_agent_callback`) flushes the just-finished session to the service
with ADK's `add_session_to_memory()`, **throttled** per session
(`_throttle.try_claim`: 120 s cooldown — tunable via `LHA_FORK_COOLDOWN` —
plus a fixed 50/session cap). Without this the next session has nothing for
`PreloadMemoryTool` to surface.

`content_sanitizer.sanitize_content_for_memory` is a separate writer-side
guard used by the dream pass only — it degrades non-image/PDF inline blobs
to a text placeholder so Memory Bank's extractor doesn't 400 on them.

## 3. The self-improvement loop (the custom heart)

All three forks run **fire-and-forget** via `SiblingAgentPlugin`
(`horizon/memory/sibling_agent_plugin.py`, registered as an `App` plugin). It
owns a throwaway runner per sibling and an `asyncio.Task` lifecycle;
siblings run under the **parent's `(app_name, user_id)`** so their writes
land where the parent's next-turn `PreloadMemoryTool` query will find them.
`close()` drains in-flight siblings for up to ~4 s at shutdown (capped
under ADK's 5 s plugin-close budget) so a turn's writes still land on
SIGTERM. The parent's user-facing response is **never blocked** by any of
these.

- **Review fork** (`horizon/memory/review_fork.py`, `after_agent_callback`,
  gated by `LHA_REVIEW_FORK`, throttled). After each turn it hands a
  restricted Gemini agent (`gemini-3.7-flash`) the conversation as a
  `<CONVERSATION>…</CONVERSATION>` snapshot plus a review prompt
  (`horizon/memory/review_prompts.py` — memory-only or combined memory+skill,
  picked by whether the session touched skills). Toolset is whitelisted to
  `memory` + skill read/write (`write`/`edit` restricted to
  `.agents/skills/<name>/…`) + `load_skill`. Recursion guard is structural: the fork's
  agent has no `after_agent_callback` chain.

- **Flush fork** (`horizon/memory/flush_fork.py`, gated by
  `LHA_PRE_COMPRESS_FLUSH`). Fired by `HorizonSummarizer` (see §5) **right
  before** ADK compacts old events — a sharper sibling whose only tool is
  `memory`, asked to rescue durable user facts before they're discarded
  into a summary. Not throttled (fires at most once per compaction).

- **Throttling** lives in `horizon/memory/_throttle.py`; `auto_capture` and
  `review_fork` share it. The flush fork and the dream pass deliberately do
  **not** use it (each is once-per-compaction / on-demand).

## 4. The nightly "dream" pass

`POST /scheduler/dream-review` (`horizon/scheduler/dream_review_endpoint.py`)
→ `horizon/memory/dream_review.py`. The cron sends an **empty** `user_ids`,
which means "every user with a real (non-scheduler) session in the lookback
window" — discovered by `list_active_users` (window =
`LHA_DREAM_LOOKBACK_HOURS`, default 24 h). The `app_name` comes from
`app.state.runner` (where sessions are actually keyed), not the request body.

For each user, `_run_dream_review_for_user` loads their most recent sessions
(`LHA_DREAM_SESSION_LIMIT`, default 50), filters to text-bearing events, and
calls Memory Bank's **`memories.generate`** once. That single call does two
things server-side:

1. **Consolidates a native Structured Profile** — schema in
   `horizon/infrastructure/memory_config.py` (`summary` / `role` / `interests` /
   `working_style` / `durable_facts`, one per `(app_name, user_id)`),
   applied to the Agent Engine resource by `scripts/provision_agent_engine.py`. The
   live agent reads it back with `retrieve_profiles`
   (`horizon/memory/user_profile.py:load_user_profile`), loaded once per session
   into `state['user_profile']` and rendered as a `## User Profile` block in
   the volatile prompt tier. That block carries the **narrative fields only**
   (`summary` / `role` / `working_style`); `interests` and `durable_facts` hold
   largely the content relevance-ranked `search_memory` already returns on
   demand via `PreloadMemoryTool`, so injecting them would re-send every fact
   on every turn. The tradeoff is that retrieval is query-conditioned, so an
   always-on fact surfaces only on a turn it matches. A profile with **no**
   narrative field set renders its lists rather than nothing. Surfaces that
   show the whole profile (`/lha/memories`) pass `include_lists=True`.
2. **Consolidates general memories** (dedupe + contradiction
   reconciliation), unless `LHA_MEMORY_CONSOLIDATION=0` (then
   `disable_consolidation=True` — Memory Bank just appends). The pass
   returns per-run `consolidation: {ran, created, updated, deleted}` counts.

Master switch: `LHA_DREAM_REVIEW=0` makes every path return
`{success: false}`. The same pass is callable on-demand via the
**`/dream-review` slash command** (`horizon/commands/__init__.py`) — a user
command, not an LLM tool.

## 5. Pre-compaction flush wiring

`HorizonSummarizer` (`horizon/context/summarizer.py`) subclasses ADK's
`LlmEventSummarizer`. In `maybe_summarize_events` it (a) calls
`spawn_flush_fork(...)` against the soon-to-be-compacted events and (b)
prepends a `[CONTEXT COMPACTION — REFERENCE ONLY]` banner to the produced
summary. It reads the memory-service + sibling-plugin handles from the
`CompactionContext` ContextVar (`horizon/context/compaction_context.py`),
populated in `on_session_start_callback`. So compaction itself triggers the
flush; the summarizer never touches Memory Bank directly.

## 6. Skills vs memory

`horizon/memory/skill_curator.py` and `horizon/memory/skill_telemetry.py` live in
this directory but drive **skill-library** promotion, not the memory store
proper (related self-improvement, different concern):

- `skill_telemetry_callback` (`after_tool_callback`) bumps per-session
  `views` / `manages` counters in `state['skill_telemetry']` when the agent
  loads or edits a skill.
- `skill_curator_callback` (`after_agent_callback`) reads those counters and
  writes promotion/review **marker entries** into Memory Bank
  (`[skill_curator] …`, thresholds: ≥3 edits ⇒ promote, ≥5 views & 0 edits
  ⇒ flag for review). It rides Memory Bank because that's the cross-session
  store, but its subject is skills, not user facts.

`horizon/memory/memory_list.py` is read-only — it powers the chat UI's memory
panel (`/lha/memories`), walking either the in-memory store or Vertex
`retrieve`, parsing `[user]`/`[agent]`/`[user_profile]` markers, and pinning
the Structured Profile to the top.

## 7. Wiring map (how to lift it)

| Piece | ADK hook / entry point | File |
|---|---|---|
| Memory surfaced each turn | `tools=[PreloadMemoryTool()]` → `search_memory` | `horizon/agent.py` |
| `memory` tool | `tools=[memory]` | `horizon/memory/add_memory_tool.py` |
| Post-turn flush | `after_agent_callback` (throttled) | `horizon/memory/auto_capture.py` |
| Review fork (judge) | `after_agent_callback` (throttled, fire-and-forget) | `horizon/memory/review_fork.py` |
| Pre-compaction flush | fired by `HorizonSummarizer` (compaction hook) | `horizon/memory/flush_fork.py` |
| Sibling runner lifecycle | `App(plugins=[SiblingAgentPlugin()])` | `horizon/memory/sibling_agent_plugin.py` |
| Skill telemetry | `after_tool_callback` | `horizon/memory/skill_telemetry.py` |
| Skill curator | `after_agent_callback` | `horizon/memory/skill_curator.py` |
| Dream pass | `POST /scheduler/dream-review` (cron) + `/dream-review` slash command | `horizon/memory/dream_review.py` |
| Structured Profile schema | applied to the Agent Engine resource at provision time | `horizon/infrastructure/memory_config.py`, `scripts/provision_agent_engine.py` |
| Profile read-back | `on_session_start_callback` → `state['user_profile']` | `horizon/memory/user_profile.py` |

`after_agent_callback` order is the contract:
`auto_capture` → `skill_curator` → `review_fork` (see `horizon/agent.py`).

## 8. Honesty callouts

- **Structured Profile + general-memory consolidation require a Vertex
  Memory Bank engine with *generation* enabled.** `_run_dream_review_for_user`
  returns `{success: false, reason: "structured profiles require Vertex
  Memory Bank"}` for any other service (e.g. `InMemoryMemoryService`), so
  none of §4 runs in tests/local. Empirically, the **dev** engine
  (`lha-agent-engine`) 404s on `generate`/`ingest` while `retrieve` works —
  so dream-review writes can't be exercised there; profile read-back can.
- **The flush fork is best-effort and timing-bound.** It's fire-and-forget
  and drained only ~4 s at shutdown; a hard kill (SIGKILL/OOM) before the
  sibling finishes loses that turn's flush. Same caveat as the sandbox
  snapshot path.
- **Dedup is exact-text only.** `memory` / `skill_curator` dedup via an
  exact `search_memory` text match; near-duplicates still write. Real
  dedup/contradiction-handling only happens in the nightly consolidation
  pass.
- **Backend-specific memory access is confined to one abstraction** —
  `horizon/memory/adapter.py` (`MemoryAdapter` Protocol + `memory_adapter()`
  factory). It owns the only `isinstance(VertexAiMemoryBankService)` /
  `InMemoryMemoryService` checks and the only private-attr reaches
  (`_session_events`, `_get_api_client`, `_agent_engine_id`), which ADK's
  `BaseMemoryService` forces because it exposes no list-all / profile API.
  Callers (`user_profile`, `dream_review`, `memory_list`) name no concrete
  service class; a non-Vertex backend degrades through `NoopMemoryAdapter`.
  Revisit the private reaches if ADK adds public APIs.
- **Gates default ON** but are easy to silence in eval baselines:
  `LHA_REVIEW_FORK`, `LHA_PRE_COMPRESS_FLUSH`, `LHA_DREAM_REVIEW`,
  `LHA_MEMORY_CONSOLIDATION` (all `=0` to disable), `LHA_FORK_COOLDOWN=0`
  (disable throttle cooldown).

## Troubleshooting

Debug by symptom. Each row points at the code that owns the behavior.

| Symptom | Where to look | Why |
|---|---|---|
| `## User Profile` block empty / never updates | `dream_review._run_dream_review_for_user`, `LHA_DREAM_REVIEW`; read-back `user_profile.load_user_profile` | The profile is written only by the nightly `memories.generate` pass; `LHA_DREAM_REVIEW=0` makes every path return `{success:false}`, and a non-Vertex service returns `{reason:"structured profiles require Vertex Memory Bank"}`. |
| Profile still empty on a real Vertex engine | §8 honesty callout (dev engine) | The dev engine (`lha-agent-engine`) 404s on `generate`/`ingest`; only `retrieve` works, so dream writes can't be exercised there. |
| `memory` succeeds but next session surfaces nothing | `auto_capture_callback` + `_throttle.try_claim` (120 s cooldown, 50/session cap, `LHA_FORK_COOLDOWN`) | Surfacing depends on the post-turn flush, which is throttled; `PreloadMemoryTool` only surfaces what was flushed. |
| Duplicate / near-duplicate memories pile up | `memory` exact-text dedup via `search_memory`; nightly consolidation `LHA_MEMORY_CONSOLIDATION` | Dedup is exact-text only; real dedup + contradiction reconciliation happen only in the dream pass. |
| Review / flush fork never runs | `LHA_REVIEW_FORK`, `LHA_PRE_COMPRESS_FLUSH`; `sibling_agent_plugin` (fire-and-forget, ~4 s drain at shutdown) | Both are gated and best-effort; a hard kill (SIGKILL/OOM) before the sibling finishes loses that turn's write. |
| `memory` rejects content | `_content_safety.scan_memory_content` | Memory is replayed into future turns, so invisible-unicode / prompt-injection / exfil patterns are refused at this untrusted-input boundary. |
| No memory at all in tests/local | `fast_api_app.build_runner` service resolve; `InMemoryMemoryService` vs `VertexAiMemoryBankService` | The whole layer no-ops cleanly when `memory_service is None` or non-Vertex; §4 never runs locally. |

## Pointers

- Agent guidance doc (repo root) — Development Rule 4 (memory model), the
  ADK callback-wiring table, and the `LHA_DREAM_*` / `LHA_MEMORY_*`
  environment-variable reference.
- `docs/architecture.md` — memory in the overall architecture overview.
- `docs/sandbox-lifecycle.md` — the companion walkthrough for the sandbox.

---

## Where to go next

- [`docs/architecture.md`](architecture.md) — where the memory layer sits in the overall system.
- [`docs/routines.md`](routines.md) — routines reuse this Memory Bank + scheduler plumbing for unattended runs.
- [`docs/sandbox-lifecycle.md`](sandbox-lifecycle.md) — the durable-state story for *files* (snapshots), the workspace analog of memory.
- [`docs/security-model.md`](security-model.md) — memory is an untrusted-input boundary; the content-safety scan ties in here.
- [`../AGENTS.md`](../AGENTS.md) — Development Rule 4 + the `LHA_DREAM_*` / `LHA_MEMORY_*` env reference.
