# Retail Product Search (Skill Plugin)

Semantic product search skill plugin on Google Cloud (Vector Search on Gemini Enterprise Agent Platform,
BigQuery, embeddings). Use to build e-commerce search, catalog discovery, or
shopping assistant agents.

> **Looking for the standalone deployable cloud agent (Agent Engine, Gemini Enterprise App, Cloud Run, & Agent Garden)?**
> See [`contrib/python/retail-product-search`](../../../../contrib/python/retail-product-search/README.md).

## Skill Surfacing Personas & Boolean Config

This plugin provides boolean configuration flags (`SURFACE_GEMINI_SKILL`, `SURFACE_AGENTS_SKILL`) in [`assets/design-spec.md`](assets/design-spec.md), [`.env.example`](.env.example), and [`Makefile`](Makefile) so you can choose which skill surfaces to enable:

| Persona / Surface | Boolean Flag | Default | What It Does |
|---|---|---|---|
| **Gemini Enterprise Desktop & Gemini CLI** | `SURFACE_GEMINI_SKILL` (`surface_gemini_skill`) | `true` | Installs a clean skill folder to `~/.gemini/skills/retail-product-search` and symlinks `~/.gemini/config/skills/retail-product-search` |
| **AI Coding Assistants (`.agents/skills`)** (ADK, Claude Code, Antigravity) | `SURFACE_AGENTS_SKILL` (`surface_agents_skill`) | `true` | Symlinks the skill into `~/.agents/skills/retail-product-search` |

## Install

### Option 1: Install via Boolean Config (`make surface`)

Toggle whichever skill personas you want to surface:

```bash
# Default: surface to both ~/.gemini/skills and ~/.agents/skills
make surface

# Surface only to Gemini Enterprise Desktop / Gemini CLI (~/.gemini/skills):
make surface SURFACE_GEMINI_SKILL=true SURFACE_AGENTS_SKILL=false

# Surface only to ~/.agents/skills:
make surface SURFACE_GEMINI_SKILL=false SURFACE_AGENTS_SKILL=true
```

### Option 2: Install via `npx skills add`

Install directly into your AI coding assistant (Claude Code, Antigravity,
Codex, ...) via `npx skills add`:

```bash
npx skills add google/adk-recipes --skill retail-product-search
```

### Option 3: Developer Install

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/plugins/retail/skills/product-search
uv sync
```

## Prerequisites

- Python 3.11+
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) with ADC
  configured (`gcloud auth application-default login`)
- A GCP project with billing enabled and BigQuery + Gemini Enterprise Agent Platform APIs on:
  ```bash
  gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com vectorsearch.googleapis.com
  ```

## Run Each Persona

### Persona 1: Gemini Enterprise Desktop App / Gemini CLI

1. Run `make surface SURFACE_GEMINI_SKILL=true`.
2. In your Gemini Enterprise Desktop app, open **Settings → General → Skills Folders**, add `~/.gemini/skills`, and check the **Skills** tab (`retail-product-search` appears automatically).
3. In chat, prompt:
   ```
   Use the retail-product-search skill to set up a product search agent on Google Cloud.
   ```

### Persona 2: AI Coding Assistants (Claude Code / Antigravity / Gemini CLI)

In a fresh workspace, launch your AI coding agent and trigger the skill.

**Claude Code:**

```
/retail-product-search
```

**Antigravity / Gemini CLI:**

```
Use the retail-product-search skill to set up a product search agent on Google Cloud.
```

The agent walks Q-MODE, runs `scripts/bootstrap.sh` to create the venv, then
`scripts/setup.py` to validate the catalog, ingest to BigQuery, and create
the Vector Search collection. Once setup finishes, launch the ADK web UI:

```bash
.venv/bin/adk web "$SKILL_DIR/scripts" --port 8765
```

Then open [http://localhost:8765](http://localhost:8765).

### Persona 3: Direct CLI Setup (No Coding Assistant)

Provision the BigQuery dataset and Vector Search 2.0 collection directly using the `Makefile`:

```bash
make setup PROJECT_ID=your-gcp-project-id REGION=us-central1
```

### Persona 4: Managed Cloud Agent (Agent Engine, Gemini Enterprise, Cloud Run, Agent Garden)

To deploy and surface the standalone agent to **Gemini Enterprise Agent Platform (Agent Engine)**, **Gemini Enterprise (GE) App**, **Cloud Run**, or **Agent Garden**, use the companion agent recipe in [`contrib/python/retail-product-search`](../../../../contrib/python/retail-product-search/README.md):

```bash
cd ../../../../contrib/python/retail-product-search
make surface DEPLOY_AGENT_ENGINE=true PUBLISH_GEMINI_ENTERPRISE=true PUBLISH_AGENT_GARDEN=true DEPLOY_CLOUD_RUN=false
```

### Which mode?

- **Quick (2 questions):** GCP project + catalog source. Silently defaults to
  the `us-central1` region and the `Extended` fields preset. Right choice for
  a first run or a demo.
- **Full (4 questions):** adds a fields-level question (`Basic` / `Standard` /
  `Extended` / `Full`) and a region confirmation. Note: `us-central1` is
  currently the only region where Vector Search 2.0 works, so the region
  question is effectively fixed — Full is really about choosing which columns
  from your CSV get indexed.

Both modes accept a custom CSV path or `gs://` URI at Q-B — you don't need
Full to use your own catalog.

## Use your own catalog

Point Q-B at a local CSV or `gs://` URI. Column requirements depend on the
fields level (Quick uses `Extended` by default):

| Level | Required | Optional |
|---|---|---|
| `Basic` | `product_id, name, price, description` | — |
| `Standard` | same required | `category, brand, image_url` |
| `Extended` (Quick default) | same required | `category, brand, image_url, rating, stock, manufacturer` |
| `Full` | same required | Extended's optional set + `variants, tags, specifications, reviews` |

Extra columns outside the chosen level's schema are rejected by
`validate_schema.py` — pick the level that matches (or exceeds) your CSV.

## Cleanup

In the agent chat:

```
clean up the GCP resources
```

Runs `cleanup.py --confirm` to delete the BigQuery dataset and Vector Search
collection.

## Troubleshooting

| Error | Fix |
|---|---|
| `MethodNotImplemented: 501` from Vector Search | `VECTOR_SEARCH_COLLECTION` has a newline. Re-export on one line |
| `ModuleNotFoundError: google.adk` | `pip install -e "$SKILL_DIR"` — google-adk is an unconditional dependency, no `[adk]` extra needed |
| `Package requires Python: 3.9.X` | Recreate venv with `python3.12 -m venv .venv` |
| `BILLING_DISABLED` / `PERMISSION_DENIED` | GCP project setup — see [references/troubleshooting.md](references/troubleshooting.md) |

Full table: [references/troubleshooting.md](references/troubleshooting.md).

## What gets built

- BigQuery dataset `retail_skill_products.products`
- Vector Search collection `retail-skill-products-collection` on Gemini Enterprise Agent Platform in
  `us-central1`, with auto-embeddings via `gemini-embedding-001`
- Workspace venv with the skill installed editable + a `design-spec.md`

The skill's source code stays in the install directory — nothing is copied
to your workspace.

## Try it

Open [http://localhost:8765](http://localhost:8765) and paste the queries below.

**Sanity check first — confirm the right catalog got ingested:**

```
List all products in the catalog, just product_id and name.
```

The IDs you see here tell you which CSV setup actually used. If you passed
your own CSV at Q-B and see different IDs than expected, setup silently
fell back to the bundled `assets/sample-products.csv`.

**Then exercise semantic search — these queries never appear verbatim in any product description:**

| What you're testing | Query |
|---|---|
| Intent → product (concept bridge) | `what should I get to reduce wrist strain while working?` |
| Constraint + intent | `budget-friendly desk accessories under $50` |
| Multi-product bundle | `setting up a home podcast studio, what do I need under $600?` |
| Pure semantic (no keyword overlap) | `what would help me switch between sitting and standing throughout the workday?` |
| Ask the agent to explain itself | `Why did you pick those results? What matched?` |

If the "explain yourself" prompt cites specific product features from the
descriptions (not just names), Vector Search retrieval + the LLM's
reasoning are both working. If it hallucinates products that don't exist
in your catalog, that's a bug worth filing.

**Failure modes to watch for:**

- Query returns 0 hits when the answer clearly exists → embeddings didn't
  index. Check the `ingest_vertex_search.py` output in the setup log for a
  `501 MethodNotImplemented` (wrong region) or an aborted indexing wait.
- Query returns only literal keyword matches, misses concept-adjacent
  products → your Vector Search collection may still be indexing. Wait
  5-10 min after setup completes and try again.
- Agent invents products with IDs you didn't ingest → LLM hallucination.
  Sharpen the `INSTRUCTION` in `scripts/agent.py` to require citing tool
  output verbatim.

## License

Apache 2.0
