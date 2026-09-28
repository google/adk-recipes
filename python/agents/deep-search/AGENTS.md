# Deep Search — Iterative Research Agent with Citations

## Intent

A clone-and-study **fullstack research agent** built on ADK + Gemini. Unlike the
RAG recipes, the interesting part **is the agent**: `app/agent.py` builds a
**multi-agent graph** that plans (with a **Human-in-the-Loop** approval step),
runs an iterative **search → critique → refine** loop until a quality bar is met,
then composes a report with **inline citations** back to web sources. It ships a
React frontend over an ADK-powered FastAPI backend. There is no datastore, no
ingestion pipeline, and no Terraform — all "data" comes from the built-in
`google_search` tool at runtime.

## When To Use

- The user wants a "deep research" agent that **plans, searches, self-critiques,
  and refines** iteratively rather than answering in one shot.
- The user wants **human-in-the-loop plan approval** before autonomous execution.
- The user wants **inline source citations** linking each claim back to a web URL.
- The user wants a **fullstack** (React + FastAPI) example with a custom research
  timeline UI, or a headless multi-agent ADK orchestration to copy.

## Eval

- **Scenarios Path**: none — this recipe ships **no eval datasets** and no eval
  config: no `tests/eval/`, no `eval_config.yaml`, and no LLM-judge scoring.
- The only test is `tests/test_runnability.py` — a **smoke test** that imports
  `app.agent` (patching `google.auth.default`) and asserts `root_agent` and `app`
  are defined. It is **not** wired into a `make test` target.
- **To add eval:** create a `tests/eval/` directory with `datasets/` + an
  `eval_config.yaml` (see the RAG recipes for the `agents-cli` eval format) and
  run it via the agents CLI. Nothing here does that today — do not assume an eval
  path exists.

## End-to-end flow

```
user topic
  -> interactive_planner_agent (root LlmAgent)          # Phase 1: Plan & Refine (HITL)
       calls plan_generator (AgentTool) to draft a plan
       refines with user; waits for EXPLICIT approval
       |  (approved)
       v
  research_pipeline (SequentialAgent)                    # Phase 2: Autonomous research
       1. section_planner            -> markdown outline           (state: report_sections)
       2. section_researcher         -> first-pass web research    (state: section_research_findings)
       3. iterative_refinement_loop (LoopAgent, max = max_search_iterations)
            research_evaluator       -> grade pass/fail + follow_up_queries (state: research_evaluation)
            escalation_checker       -> stop loop when grade == "pass"
            enhanced_search_executor -> run follow-ups, merge findings
       4. report_composer_with_citations -> report with <cite source="src-N"/> tags
            citation_replacement_callback rewrites tags -> Markdown links (state: final_report_with_citations)
```

The loop stops early when `research_evaluator` grades `pass` (the custom
`EscalationChecker` escalates), otherwise after `max_search_iterations` passes.

## Most interesting files to study (in order)

### Backend — the multi-agent orchestration (the bulk of the logic; read first)
1. **`app/agent.py`** — **the heart of the recipe.** Unlike the RAG recipes, this
   is *not* a thin wrapper; it defines the whole graph and all the interesting
   behavior:
   - **`interactive_planner_agent`** (root `LlmAgent`) — the HITL entry point. It
     never answers directly: it calls the `plan_generator` `AgentTool` to draft a
     plan, refines it against user feedback, and only delegates to
     `research_pipeline` on **explicit** approval. Its `output_key` is
     `research_plan`.
   - **`plan_generator`** — builds a 5-goal, tag-prefixed plan (`[RESEARCH]` /
     `[DELIVERABLE]`, plus `[MODIFIED]`/`[NEW]`/`[IMPLIED]` refinement tags);
     search is strictly limited to topic disambiguation.
   - **`research_pipeline`** (`SequentialAgent`) — `section_planner` →
     `section_researcher` → `iterative_refinement_loop` → `report_composer`.
   - **`iterative_refinement_loop`** (`LoopAgent`,
     `max_iterations=config.max_search_iterations`) — `research_evaluator`
     (emits a `pass`/`fail` grade + `follow_up_queries` via the `Feedback`
     Pydantic schema) → `EscalationChecker` → `enhanced_search_executor`.
   - **`EscalationChecker`** — a **custom `BaseAgent`** whose only job is loop
     control: it `yield`s an `escalate=True` event when the last evaluation
     graded `pass`. This is the key trick for terminating a `LoopAgent` on a
     data-driven condition.
   - **`collect_research_sources_callback`** / **`citation_replacement_callback`**
     — the citation machinery. The first harvests `grounding_metadata` (URLs,
     titles, domains, confidence) into `state["sources"]` +
     `state["url_to_short_id"]`; the second rewrites `<cite source="src-N"/>`
     tags into Markdown links and drops invalid ones.
2. **`app/config.py`** — the `ResearchConfiguration` dataclass: `worker_model`
   and `critic_model` (both read from the `MODEL_NAME` env var) and
   `max_search_iterations` (default `5`). Also the auth bootstrap — AI Studio via
   `GOOGLE_API_KEY`, else fall back to `google.auth.default()` + Vertex AI.

### Frontend (custom research UI)
3. **`frontend/src/App.tsx`** — the backend↔UI contract. Consumes the ADK
   `/api/run_sse` stream and routes events by **agent name**: `getEventTitle()`
   maps each agent to a timeline label, website counts come from
   `section_researcher`/`enhanced_search_executor`, and the final report is
   captured from `report_composer_with_citations`. This is exactly why renaming
   agents in `agent.py` breaks the UI.
4. **`frontend/src/components/`** — `ActivityTimeline.tsx` renders the live
   research timeline, `ChatMessagesView.tsx` renders chat + the final cited
   report, `WelcomeScreen.tsx`/`InputForm.tsx` handle input. `ui/` is Shadcn.

### Plumbing (thin layer — read last)
5. **`app/__init__.py`** — env bootstrap: loads `.env`, resolves the GCP project,
   sets Vertex defaults, then imports `root_agent`. The import is intentionally
   below the bootstrap because the agent reads env vars **at import time**.
6. **`tests/test_runnability.py`** — the sole test: a smoke import asserting
   `root_agent`/`app` exist (with `google.auth.default` patched so no ADC is
   needed).

## Data handling

- **No corpus, no embeddings, no ingestion.** All information is fetched live via
  the built-in `google_search` tool. There is nothing to provision.
- **Session state is the data bus.** Agents communicate through state keys, not
  return values — trace the flow by reading `output_key`s and `{state}` template
  refs in `agent.py`: `research_plan` → `report_sections` →
  `section_research_findings` → `research_evaluation` → `sources` /
  `url_to_short_id` → `final_cited_report` → `final_report_with_citations`.
- **Citations** are derived from Gemini **grounding metadata**:
  `collect_research_sources_callback` assigns each URL a short id (`src-N`), the
  composer references those ids with `<cite source="src-N"/>`, and
  `citation_replacement_callback` resolves them to Markdown links in-line (no
  separate "References" section).
- **Human-in-the-loop:** nothing is researched until the user explicitly approves
  the plan in chat.

## Gotchas / things to know

- **Agent names are a public API.** The frontend keys off exact names
  (`plan_generator`, `section_planner`, `section_researcher`,
  `enhanced_search_executor`, `report_composer_with_citations`,
  `interactive_planner_agent`). Rename any of them in `app/agent.py` and you
  **must** update `frontend/src/` (mainly `App.tsx` / `ChatMessagesView.tsx`).
- **Models come from `MODEL_NAME`, not hardcoded.** `.env.example` sets
  `MODEL_NAME=gemini-3.5-flash`; both `worker_model` and `critic_model`
  read the same var. If `MODEL_NAME` is unset, `config.py` returns `None` for the
  models — set it (the smoke test defaults it to `gemini-3.5-flash`). Do not use
  deprecated `gemini-2.0-flash` / `gemini-2.5-flash`.
- **AI Studio by default, Vertex optional.** With `GOOGLE_API_KEY` set it uses AI
  Studio; otherwise it falls back to `google.auth.default()` + Vertex, so a
  credential-less import fails in Vertex mode.
- **The refinement loop is bounded.** It stops early on a `pass` grade (via
  `EscalationChecker`) or after `max_search_iterations` (default `5`) — whichever
  comes first. Raising the cap raises latency and cost.
- **No `make test` and no eval.** `make lint` runs codespell + ruff + mypy; run
  the single smoke test manually with `uv run pytest tests/test_runnability.py`.
- **`make dev` runs two servers.** It backgrounds `dev-backend` (ADK
  `api_server` on `:8000`) and `dev-frontend` (Vite on `:5173`); Vite proxies
  `/api/*` to `http://127.0.0.1:8000` (base path `/app/`).

## Where to run things

`Makefile` targets: `make install` (`uv sync` + `npm --prefix frontend install`),
`make dev` (backend + frontend together), `make dev-backend`
(`adk api_server app` on `:8000`), `make dev-frontend` (Vite dev server on
`:5173`), `make playground` (`adk web` on `:8501` — ADK's built-in UI, no React),
`make lint` (codespell + `ruff check` + `ruff format --check` + `mypy`). There is
**no `make test`**: run `uv run pytest tests/test_runnability.py` directly. There
is **no eval**.

## Reuse (copy as-is)

- **`app/`** is a self-contained ADK agent — copy the directory and the whole
  multi-agent graph, config, and citation callbacks travel together. Its only
  runtime inputs are env vars (`MODEL_NAME`, plus either `GOOGLE_API_KEY` or
  Vertex AI credentials). No datastore, ingestion, or Terraform to stand up.
- **`frontend/`** is an independent React + Vite app (its own `package.json`) —
  copy it, but it is **coupled to the backend by agent names** and the ADK SSE
  contract (`/api/run_sse`, `/api/apps/app/...`). Keep those names in sync with
  `app/agent.py`.
- The agent also runs **headless**: point any ADK runtime at
  `app.agent:root_agent` (that's exactly what `make playground` does via
  `adk web`), no React required.
