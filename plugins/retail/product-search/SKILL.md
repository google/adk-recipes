---
name: retail-product-search
description: >-
  Creates product search agents with semantic search and RAG on Google Cloud
  (Vertex AI Vector Search, BigQuery, embeddings). Use when the user wants to
  "build a product search agent", "create an e-commerce search", "make a
  shopping assistant", "set up semantic catalog discovery", "ingest products
  into Vector Search", or "deploy a retail RAG agent". Handles the full
  pipeline: catalog data ingestion to BigQuery, Vertex AI Vector Search
  collection setup, ADK agent scaffolding, evaluation, and Cloud Run deployment.
metadata:
  author: Google
  license: Apache-2.0
  version: 0.2.0
---

# Product Search Agent

Creates product search agents with semantic search and RAG on Google Cloud.

## STOP — Q-MODE FIRST

**If a catalog is already loaded** (system context says "DEPLOYED search agent"
or provides a `<catalog>` block), skip Q-MODE and answer product queries
directly using the catalog.

**Otherwise**, your first message MUST be exactly this:

```
[skill: retail-product-search] active.
Q-MODE: Pick a setup mode? [default: 1]
  1. Quick start -- 2 questions, smart defaults, ~60s. Best for demos and first-timers.
  2. Full setup  -- 4 questions, ~2 min. Best for real builds.
```

Then stop and wait. Accept `1`, `quick`, empty/Enter (Quick), or `2`, `full` (Full).

## Execution Rules

1. Q-MODE first. No preamble, no plan-proposing.
2. One question at a time. Show `[default: ...]`. Empty input = default.
3. Save answers to `./design-spec.md` in the workspace as you collect them.
4. After interview, run `scripts/setup.py` (see Workspace Setup below).
5. User can say "configure more" mid-Quick-Start to switch to Full.

## Workspace Setup

The skill has two locations:
- **Install dir** — where SKILL.md and scripts live (varies by host)
- **Workspace** — the agent's cwd; design-spec.md, .venv, and per-run state live here

By the end of this section the workspace must have `.venv/` (with the skill
installed editable), `design-spec.md`, and `SKILL_DIR` exported in the shell.

Run this as ONE shell command — splitting it across tool calls loses state:

```bash
SKILL_DIR=$(for d in ~/.claude/skills ~/.agents/skills ~/.gemini/skills ~/.cursor/skills; do
  [ -f "$d/retail-product-search/SKILL.md" ] && echo "$d/retail-product-search" && break
done)
bash "$SKILL_DIR/scripts/bootstrap.sh"
```

`bootstrap.sh` finds a Python 3.10+ interpreter (with absolute-path fallback
for sandboxed shells), creates `.venv`, installs the skill editable, and
copies `design-spec.md` into the workspace.

All scripts run from the install dir against the workspace config. **Use
`.venv/bin/python`, not bare `python`** — bare `python` may resolve to a
Python without the skill's editable install on sys.path.

```bash
.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md
.venv/bin/python "$SKILL_DIR/scripts/cleanup.py" --config ./design-spec.md --confirm
```

Details in [references/install-paths.md](references/install-paths.md).

## Skill Dependencies

This skill works in conjunction with the following core Google Cloud skills:
- `bigquery-basics` (for database configuration guidelines)
- `gemini-api` (for Vertex AI API / Google Gen AI SDK best practices)

Verify if these skills are installed in your active skills directory. If they are missing, recommend the developer to install them by running:
`npx skills add google/skills --skill bigquery-basics gemini-api`

## Mode 1: Quick Start (2 questions)

| Q | Question | Default |
|---|---|---|
| Q-A | GCP project ID? | `$GOOGLE_CLOUD_PROJECT` or `gcloud config get-value project` |
| Q-B | Where's your product data? | `assets/sample-products.csv` (bundled) |

Accepted for Q-B: empty / `default` (bundled), `/path/to/file.csv`, or `gs://...`.

Silent defaults: Extended fields, `us-central1`.

After Q-A and Q-B, do this automatically (don't ask the user to copy/paste).
**Run these steps SEQUENTIALLY — do not parallelize.** Steps 2-3 modify the
file bootstrap copies in step 1; running them concurrently is a race.

1. **Run bootstrap first and wait for completion.** `bash "$SKILL_DIR/scripts/bootstrap.sh"`
   copies the YAML-frontmatter design-spec template into the workspace at
   `./design-spec.md`. Do NOT touch `./design-spec.md` until bootstrap exits.
2. **Mutate the existing `./design-spec.md`** — do NOT rewrite it from scratch.
   `setup.py` parses YAML frontmatter via `_setup_utils.py`. A Markdown-only
   file fails with `'NoneType' object has no attribute 'get'`. Use Edit / sed
   to replace specific lines:
   - `gcp_project_id: ""` → `gcp_project_id: "<Q-A answer>"`
   - `data_source: assets/sample-products.csv` → `data_source: <Q-B answer>` (only if user gave a non-default)
3. Say: "Taking defaults for the rest. Running setup — this takes 2-5 min to
   create a BigQuery dataset and Vector Search collection. Say 'configure
   more' to switch to Full setup."
4. Run `.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md`
5. Stream output. On non-zero exit, surface the error and check
   [references/troubleshooting.md](references/troubleshooting.md)
6. On success, set `VECTOR_SEARCH_COLLECTION` and proceed to Test

## Mode 2: Full Setup

Adds two more questions: product fields level and GCP region.

| Q | Question | Default | Notes |
|---|---|---|---|
| Q-fields | Product fields level | `Extended` | `Basic` / `Standard` / `Extended` / `Full`. Match this to your CSV's columns. Don't offer "Custom" — `validate_schema.py` rejects it. |
| Q-region | GCP region | `us-central1` | **Only confirmed-working region for Vector Search 2.0.** Other regions return `501 MethodNotImplemented`. |

Otherwise identical to Quick Start.

## When to Use

- E-commerce product search, shopping assistants, semantic catalog discovery

Don't use for generic document search, simple keyword search, or non-retail.

## Project Tree

```
retail-product-search/
  assets/
    design-spec.md            # Source of truth -- filled by Q-MODE
    sample-products.csv       # Bundled 5-product demo catalog
  references/                  # Deep-dive docs (load on demand)
  scripts/
    agent.py                  # Reference ADK agent
    retrievers.py             # Vector Search retrieval logic
    setup.py                  # Pipeline driver (reads design-spec.md)
    bootstrap.sh              # Workspace bootstrap (called from Workspace Setup)
    validate_schema.py
    ingest_bigquery.py
    ingest_vertex_search.py
    cleanup.py
```

Customize: rewrite `scripts/agent.py` (see
[references/agent-example.md](references/agent-example.md)) and
`scripts/retrievers.py` with your product-specific fields.

## Test

After `setup.py` succeeds, set the collection env var (one line, no newlines):

```bash
export VECTOR_SEARCH_COLLECTION="projects/$GOOGLE_CLOUD_PROJECT/locations/us-central1/collections/retail-skill-products-collection"
```

Then either:

**With ADK** (interactive UI):
```bash
# Use the WORKSPACE VENV's adk (not bare `adk`) so the skill's editable
# install is on sys.path. Bare `adk` may resolve to a global Python (pyenv,
# brew, etc.) whose ADK can't find the skill and reports an empty app list.
.venv/bin/adk web "$SKILL_DIR/scripts" --port 8765
```
Open http://127.0.0.1:8765, click `scripts`, query.

⚠️ Two things must be right:
- **Point `adk web` at `$SKILL_DIR/scripts`, not at `.`** — agent code lives
  in the install dir, not the workspace. `adk web .` fails with "No agents
  found in current folder".
- **Use `.venv/bin/adk`, not bare `adk`** — bare `adk` may launch the wrong
  Python and silently fail to load the agent (UI loads, but `/list-apps`
  returns `[]` and queries time out).

**Without ADK** (direct smoke test):
```bash
.venv/bin/python -c "from scripts.retrievers import search; print(search('laptop for video editing', top_k=3))"
```

Semantic-only retrieval — no structured filters on price, stock, or rating.
For demo queries and how to add structured filtering, see
[references/architecture.md](references/architecture.md).

## Evaluate

```bash
cd <repo-root>
./vs eval retail-product-search --project-id $PROJECT
```

`EVAL.yaml` declares `rubric` (LLM-as-judge) + `assertions` (deterministic
checks). Target: 80%+ passing.

## Deploy

**Never deploy without explicit human approval.**

Cloud Run service account needs `roles/bigquery.dataViewer` on the dataset and
`roles/aiplatform.user` on the project. Deploy via `gcloud run deploy` or your
org's existing tooling.

## Gotchas

- **No results**: collection empty or `VECTOR_SEARCH_COLLECTION` not set
- **Slow search**: check region and `top_k`
- **No structured filters**: `search()` is pure semantic similarity. Price /
  stock / currency filters happen client-side in the LLM, so results may
  include items outside the constraint. Don't promise hard filters
- **ADK session memory**: if the retriever errored in earlier turns, the
  model "learns" the tool is broken. Click "New Session" in `adk web` after
  fixing the underlying issue

## Troubleshooting

Most-common failures inline; full table in
[references/troubleshooting.md](references/troubleshooting.md).

| Error | Fix |
|---|---|
| `setup.py` exits with `'NoneType' object has no attribute 'get'` | `design-spec.md` was rewritten as plain Markdown instead of mutating the YAML-frontmatter template bootstrap copied. Wait for bootstrap to finish, then **edit** (not rewrite) `./design-spec.md` — only change the field values inside the existing `---...---` frontmatter |
| `adk web` starts but `/list-apps` returns `[]` / browser shows "No agents found" | Bare `adk` resolved to a global Python that lacks the editable install. Kill it and restart with `.venv/bin/adk web "$SKILL_DIR/scripts" --port 8765` |
| `MethodNotImplemented: 501` from Vector Search | `VECTOR_SEARCH_COLLECTION` has a newline. Re-export on one line |
| `ModuleNotFoundError: google.adk` | `pip install -e "$SKILL_DIR"` — google-adk is an unconditional dependency, no `[adk]` extra needed |
| `Package requires Python: 3.9.X` | venv used system Python 3.9. Recreate with `python3.12 -m venv .venv` |
| `BILLING_DISABLED` / `PERMISSION_DENIED` / `API has not been used` | GCP project setup — see troubleshooting.md |

## MCP Migration

This skill uses `gcloud` CLI + Python SDKs (`google-genai`,
`google-cloud-bigquery`, `google-cloud-aiplatform`). Per
[Phase 2 Skills guidelines](https://github.com/google/skills), 1p skills
should prefer remote MCP tools when available. Migration map:

| Service | Where | Future MCP |
|---|---|---|
| BigQuery | `ingest_bigquery.py`, `validate_schema.py` | BigQuery MCP |
| Vertex AI Vector Search | `ingest_vertex_search.py`, `setup.py` | Vertex AI MCP |
| Vertex AI Embeddings | `retrievers.py` | Vertex AI MCP |
| Cloud Run | `gcloud run deploy` | Cloud Run MCP |

## Completion Checklist

- [ ] Product fields level and data source confirmed
- [ ] Data ingestion ran; Vector Search populated
- [ ] `retrieve_docs` returns results in ADK web UI
- [ ] Evaluation passes success criteria
- [ ] Deployed (if beyond prototype)

## References

Load on demand:

- [references/install-paths.md](references/install-paths.md) — host install dirs, Python fallback, `bash -c` rationale
- [references/dependencies.md](references/dependencies.md) — pip deps and install quirks
- [references/architecture.md](references/architecture.md) — what retrieval does (semantic-only), demo queries, structured-filter strategies
- [references/troubleshooting.md](references/troubleshooting.md) — full error table
- [references/agent-example.md](references/agent-example.md) — how agent.py + retrievers.py fit together
- [references/ingestion-scripts.md](references/ingestion-scripts.md) — per-script CLI reference
