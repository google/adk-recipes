# Retail Product Search

Semantic product search agent on Google Cloud (Vector Search on Gemini Enterprise Agent Platform,
BigQuery, embeddings). Use to build e-commerce search, catalog discovery, or
shopping assistant agents.

## Supported Personas & Surfaces

This plugin supports both **AI Coding Assistant / Desktop Skill** workflows and **Managed Cloud Agent** deployments:

| Persona / Surface | How it is used | Entrypoint / Tooling |
|---|---|---|
| **AI Coding Assistants** (Claude Code, Antigravity, Gemini CLI, Codex) | Conversational skill that provisions BigQuery + Vector Search and scaffolds the agent in your workspace | `SKILL.md` (`npx skills add` or `make install-skill-local`) |
| **GE Desktop App** (Spark Beta, GoGo, Sobi) | Local skill in `~/.gemini/skills/retail-product-search` or synced to `google3` (`//third_party/skills/skills/`) | `make install-skill-local` / `make sync-google3-skill` |
| **Gemini Enterprise Agent Platform (Agent Engine)** | Managed ADK Reasoning Engine runtime on Google Cloud | `make deploy-agent-engine` (`agents-cli deploy`) |
| **Gemini Enterprise (GE) App** | Registered ADK / A2A agent accessible in the Gemini Enterprise web/Sobi assistant | `make publish-gemini-enterprise` (`agents-cli publish`) |
| **Cloud Run & Agent Garden** | Containerized FastAPI + A2A + Reasoning Engine HTTP server (`deployable: true`) | `make deploy-cloudrun`, `Dockerfile`, `agents-cli-manifest.yaml` |

## Install

### 1. Skill Install (Coding Assistants & GE Desktop / Spark Beta)

Install directly into your AI coding assistant (Claude Code, Antigravity,
Codex, ...) via `npx skills add`. The tool discovers `SKILL.md` from this
recipe and registers `/retail-product-search` as an invocable skill:

```bash
npx skills add google/adk-recipes --skill retail-product-search
```

Or install locally across `~/.gemini/skills/retail-product-search` (validated for **Spark Beta**), `~/.gemini/config/skills`, and `~/.agents/skills`:

```bash
make install-skill-local
```

To sync the skill to a `google3` CitC client (`//third_party/skills/skills/retail-product-search`) for GoGo / Sobi:

```bash
make sync-google3-skill
# Or from Mac to a remote Cloudtop over SSH:
make sync-google3-skill CLOUDTOP_HOST=<your-cloudtop>.c.googlers.com
```

### 2. Developer / Agent Runtime Install

If you're contributing to the recipe or deploying the agent directly:

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/plugins/retail/product-search
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

## Run

### Persona A: Run via Skill (Claude Code / Antigravity / Spark Beta)

In a fresh workspace, launch your AI coding agent and trigger the skill.

**Claude Code:**

```
/retail-product-search
```

**Antigravity / Spark Beta / Sobi:**

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

### Persona B: Direct CLI Setup (No Coding Assistant)

Provision the BigQuery dataset and Vector Search 2.0 collection directly using the `Makefile`:

```bash
make setup PROJECT_ID=your-gcp-project-id REGION=us-central1
```

### Persona C: Deploy to Agent Engine, Gemini Enterprise & Cloud Run

Once the catalog is ingested (`make setup`), deploy and surface the agent to managed Google Cloud runtimes:

1. **Deploy to Gemini Enterprise Agent Platform (Agent Engine):**
   ```bash
   make deploy-agent-engine PROJECT_ID=your-gcp-project-id REGION=us-central1
   ```
2. **Register with your Gemini Enterprise (GE) App:**
   ```bash
   make publish-gemini-enterprise \
     PROJECT_ID=your-gcp-project-id \
     GEMINI_ENTERPRISE_APP_ID="projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>" \
     AGENT_ENGINE_ID="projects/<project-number>/locations/us-central1/reasoningEngines/<reasoning-engine-id>"
   ```
3. **Deploy to Cloud Run (A2A + Reasoning Engine FastAPI Service):**
   ```bash
   make deploy-cloudrun PROJECT_ID=your-gcp-project-id REGION=us-central1
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
