---
# Product Search Agent - Design Spec
# Fill in your values and use this as input for project generation.

# --- Required ---
gcp_project_id: ""                                          # REQUIRED: your GCP project ID

# --- Data ---
data_source: assets/sample-products.csv                     # local CSV path or gs://bucket/path/products.csv
product_fields: Extended                                    # Basic | Standard | Extended | Full

# --- GCP ---
gcp_region: us-central1                                     # Vector Search 2.0 region (us-central1 is the only confirmed-working region today)
dataset_id: retail_skill_products
table_id: products

# --- Optional warnings ---
catalog_size: "1K-50K"                                      # only used to trigger a >500K Dataflow hint

# --- Skill Surfacing Personas (boolean flags) ---
surface_gemini_skill: true                                  # true | false: install to ~/.gemini/skills and ~/.gemini/config/skills (Gemini Enterprise Desktop / Gemini CLI)
surface_agents_skill: true                                  # true | false: install to ~/.agents/skills (ADK / Claude Code / Antigravity)
---

# Product Search Agent

## Overview

This design spec captures the configuration decisions for a retail product search
agent built on Google Cloud. It is generated during the SKILL.md interview and
used by ingestion scripts and agent scaffolding.

## How to Use

1. Fill in `gcp_project_id` above (or let the coding agent do it conversationally).
2. Pass this file to any script: `python scripts/ingest_bigquery.py --config assets/design-spec.md --local-file assets/sample-products.csv`.
3. CLI args always override values from this file.

## Design Decisions

Document any non-obvious choices here so future contributors understand the "why".
