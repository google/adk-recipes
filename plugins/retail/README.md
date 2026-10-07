# Retail AI Plugin (`plugins/retail`)

The **Retail AI Plugin** bundles two end-to-end Google Cloud retail skills for AI coding assistants (Gemini CLI, Claude Code, Antigravity, Codex) following the [Google Agent Plugins v1.0.0 specification][spec] (`plugin.json` + `skills/<skill-name>/SKILL.md`):

1. **[`retail-product-search`](./skills/product-search/)** — Semantic & hybrid e-commerce product catalog search powered by **Vector Search 2.0 on Gemini Enterprise Agent Platform**, **BigQuery**, `gemini-embedding-001` (768-dim embeddings), and `gemini-3.7-flash`.
2. **[`retail-virtual-tryon`](./skills/virtual-tryon/)** — Generative **image and 360° catwalk video virtual try-on** across clothing, eyewear, footwear, jewelry, and cosmetics powered by **Gemini image generation models** and **Veo** (`gemini-3.7-flash` orchestration), with an automated pre-flight product-cutout catalog scanner.

**Team:** `FDE/Blackbelt` · **Authors / Contributors:** Tanvi Singhal ([`@tanvisinghal-0105`](https://github.com/tanvisinghal-0105)), Gabriela ([`@gabrielahrlr`](https://github.com/gabrielahrlr)) · **License:** Apache-2.0

---

## Skills vs. Deployable Cloud Agents: Which Should You Use?

This repository provides the Retail capabilities in two complementary formats depending on your persona:

| Surface | Location | Target Persona | How It Works |
|---|---|---|---|
| **Agent Plugin Skills** *(this folder)* | [`plugins/retail/skills/product-search`](./skills/product-search/)<br>[`plugins/retail/skills/virtual-tryon`](./skills/virtual-tryon/) | Developers using **Gemini CLI, Claude Code, Antigravity, or Codex** | Install the skill into `~/.gemini/skills/` or `~/.agents/skills/`. Your coding assistant walks you through interactive Q-MODE configuration (`design-spec.md`), provisions BigQuery / Vector Search / GCS, and scaffolds or exports a tailored app in your workspace. |
| **Standalone Deployable Agents** | [`contrib/python/retail-product-search`](../../contrib/python/retail-product-search/)<br>[`contrib/python/retail-virtual-tryon`](../../contrib/python/retail-virtual-tryon/) | Cloud / Platform Engineers deploying to **Agent Engine, Gemini Enterprise, Agent Garden, or Cloud Run** | Ready-to-deploy ADK + FastAPI + A2A + Vertex AI Reasoning Engine microservices with `Dockerfile`, `agents-cli-manifest.yaml`, and `make surface` boolean deployment flags. |

---

## Included Skills Overview

| Skill | Directory | Key Capabilities | Google Cloud Services |
|---|---|---|---|
| **Retail Product Search** | [`skills/product-search/`](./skills/product-search/) | • Automated BigQuery dataset & Vector Search 2.0 collection setup (`scripts/setup.py`)<br>• Bundled 1,000-product sample catalog (`assets/sample-products.csv`) or custom CSV/BQ table ingestion<br>• Semantic, keyword, and hybrid RRF retrieval (`scripts/retrievers.py`)<br>• LLM-as-a-judge evaluation pipeline (`EVAL.yaml`) | Vector Search (Gemini Enterprise Agent Platform), BigQuery, Vertex AI (`gemini-3.7-flash`, `gemini-embedding-001`) |
| **Retail Virtual Try-On** | [`skills/virtual-tryon/`](./skills/virtual-tryon/) | • Pre-flight Gemini vision catalog classifier (`scripts/scan_catalog.py`) that filters out model-worn photos and auto-tags clean product cutouts<br>• Multi-category still-image try-on + Veo catwalk video animation (`scripts/tryon_processor.py`)<br>• Local interactive Try-On Studio UI (`scripts/server.py`) + standalone Cloud Run app exporter (`scripts/export_app.py`) | Vertex AI (Gemini image models, Veo, `gemini-3.7-flash`), Cloud Storage, Cloud Run |

---

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) authenticated with Application Default Credentials:
  ```bash
  gcloud auth application-default login
  export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
  ```

---

## Quickstart & Installation

### Option 1: Install via `npx skills add` (No Repo Clone Required)

Install either skill directly into your AI coding assistant (`~/.agents/skills/`):

```bash
npx skills add google/adk-recipes --skill retail-product-search
npx skills add google/adk-recipes --skill retail-virtual-tryon
```

### Option 2: Surface Both Skills Locally from `plugins/retail` (`make surface`)

You can surface both skills at once directly from this `plugins/retail/` directory without navigating into individual skill subfolders:

```bash
cd plugins/retail

# Default: surface both skills to ~/.gemini/skills and ~/.agents/skills
make surface

# Surface only to Gemini CLI / Gemini Enterprise Desktop (~/.gemini/skills):
make surface SURFACE_GEMINI_SKILL=true SURFACE_AGENTS_SKILL=false

# Surface only to ~/.agents/skills (Claude Code / Antigravity / ADK):
make surface SURFACE_GEMINI_SKILL=false SURFACE_AGENTS_SKILL=true
```

### Option 3: Run Interactive Setup & Local Testing

Once installed, open your coding assistant in any workspace directory and prompt:
- **For Product Search:** *"Use the retail-product-search skill to set up a semantic product search agent for my catalog."*
- **For Virtual Try-On:** *"Use the retail-virtual-tryon skill to build a virtual try-on studio with image and catwalk video support."*

Or provision the sample GCP resources and run the runnability tests directly from `plugins/retail/`:

```bash
cd plugins/retail

# Run runnability tests (tests/test_runnability.py) across both skills:
make test

# Provision GCP resources for Product Search or Virtual Try-On:
make setup-product-search PROJECT_ID="$GOOGLE_CLOUD_PROJECT"
make setup-virtual-tryon PROJECT_ID="$GOOGLE_CLOUD_PROJECT"
```

---

## Directory Structure

Why is there a `skills/` subfolder? This plugin follows the [Agent Plugins v1.0.0 specification][spec], which places `plugin.json` at the plugin root (`plugins/retail/plugin.json`) and packages each skill under `skills/<skill-name>/SKILL.md` so multi-skill industry plugins can be discovered and installed as a single cohesive unit:

```text
plugins/retail/
├── plugin.json                     # Agent Plugins v1.0.0 manifest & ownership metadata
├── README.md                       # Top-level Retail plugin overview & quickstart (this file)
├── Makefile                        # Top-level targets to surface, set up, and test both skills
└── skills/
    ├── product-search/             # retail-product-search skill
    │   ├── SKILL.md                # Conversational agent instructions (Q-MODE + setup workflow)
    │   ├── README.md               # Detailed product-search skill guide
    │   ├── EVAL.yaml               # Evaluation rubrics
    │   ├── Makefile                # Skill-level surface & setup targets
    │   ├── pyproject.toml          # Skill Python dependencies & project config
    │   ├── uv.lock                 # Locked dependency graph
    │   ├── assets/                 # Design spec template & 1,000-product sample CSV
    │   ├── references/             # Deep-dive docs (architecture, ingestion, troubleshooting)
    │   ├── scripts/                # BigQuery + Vector Search ingestion, retrieval & ADK agent
    │   └── tests/                  # Runnability smoke tests (test_runnability.py)
    └── virtual-tryon/              # retail-virtual-tryon skill
        ├── SKILL.md                # Conversational agent instructions (Q-MODE + setup workflow)
        ├── README.md               # Detailed virtual-tryon skill guide
        ├── EVAL.yaml               # Evaluation rubrics
        ├── Makefile                # Skill-level surface & setup targets
        ├── pyproject.toml          # Skill Python dependencies & project config
        ├── uv.lock                 # Locked dependency graph
        ├── assets/                 # Sample catalog cutouts, Studio web UI & Cloud Run export template
        ├── references/             # Virtual try-on architecture & safety reference
        ├── scripts/                # Catalog scanner, Gemini/Veo try-on processor, FastAPI server & exporter
        └── tests/                  # Runnability smoke tests (test_runnability.py)
```

## Related Links

- **Standalone Deployable Agents:**
  - [`contrib/python/retail-product-search`](../../contrib/python/retail-product-search/)
  - [`contrib/python/retail-virtual-tryon`](../../contrib/python/retail-virtual-tryon/)
- **Specifications & Handbook:**
  - [ADK Recipes Plugin Layout & Specification](../../docs/recipe-handbook/plugins.md)
  - [Agent Plugins v1.0.0 Specification][spec]

[spec]: https://agent-plugins.org/specification
