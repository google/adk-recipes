---
# Virtual Try-On Agent - Design Spec
# Fill in your values and use this as input for project generation.

# --- Required ---
gcp_project_id: ""                                          # REQUIRED: your GCP project ID

# --- GCP ---
gcp_region: us-west1                                        # region for Gemini image / Veo

# --- Try-on mode ---
tryon_mode: image_and_video                                 # image_only | image_and_video
tryon_model: flash                                          # flash (recommended) | pro
tryon_categories:                                           # product categories you intend to support
  - Clothing

# --- Buckets ---
tryon_output_bucket: ""                                     # default: {project_id}-tryon-output
tryon_upload_bucket: ""                                     # default: {project_id}-tryon-uploads

# --- Catalog ---
tryon_catalog_path: catalog_images                          # local folder, 'demo', or gs:// URI
tryon_catalog_upload: true                                  # set false to run purely against a local folder

# --- Mode 2 only: Export Web App & GCS Catalog Sync ---
# Uncomment and fill in if exporting the standalone containerized app.
# export_directory: ./vto-retail-app
# gcs_catalog_bucket: ""                                    # gs:// bucket hosting the catalog images
---

# Virtual Try-On Agent

## Overview

This design spec captures the configuration decisions for a retail virtual
try-on agent built on Google Cloud (Gemini image models and Veo on Gemini
Enterprise Agent Platform). It is generated during the SKILL.md interview
and used by setup, sandbox, and export scripts.

## How to Use

1. Fill in `gcp_project_id` above (or let the coding agent do it conversationally).
2. Pass this file to any script: `.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md`.
3. CLI args always override values from this file.

## Design Decisions

Document any non-obvious choices here so future contributors understand the "why".
