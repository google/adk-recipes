# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Container image for KFP pipeline components.
#
# This image is used as the base_image for all three KFP components
# (preprocess, chunk_and_index, cleanup). When Vertex AI Pipelines
# executes a step, it pulls this image and runs the serialized
# component function inside it.
#
# Key design decisions:
#   - python:3.11-slim (not 3.12) to match the project's uv.lock
#   - uv pip install --system (not uv sync) because KFP's executor
#     runs on the system Python (/usr/local/bin/python3), NOT inside
#     a .venv. Using uv sync would install to .venv and KFP would
#     get ModuleNotFoundError at runtime.
#   - PYTHONPATH=/app so that `from pipeline.xxx import yyy` resolves
#     to /app/pipeline/xxx.py inside the container.
#
# Build from project root (context must be repo root for COPY paths):
#   docker build -f data_ingestion_pipeline/Dockerfile -t data-pipeline:latest .
#
# Or via Cloud Build (no local Docker needed):
#   gcloud builds submit --config=data_ingestion_pipeline/cloudbuild.yaml \
#     --substitutions=_IMAGE="$PIPELINE_IMAGE" --project="$GOOGLE_CLOUD_PROJECT" .
#
# Push to Artifact Registry:
#   docker tag data-pipeline:latest \
#     ${REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${PROJECT_NAME}-pipeline/data-pipeline:latest
#   docker push \
#     ${REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${PROJECT_NAME}-pipeline/data-pipeline:latest

FROM python:3.11-slim

# Install uv for fast dependency resolution (pinned version for reproducibility)
RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /app

# Install all project dependencies into the system Python.
# We copy pyproject.toml + app/ first (app/ is needed because the project
# is defined as a package in pyproject.toml and must be installable).
# uv pip install --system targets /usr/local/lib/python3.11/ directly,
# making packages available to KFP's executor without activating a venv.
COPY pyproject.toml README.md uv.lock* ./
COPY app/ app/
RUN uv pip install --system --no-cache .

# Copy the shared src package — this is where all business logic lives.
# KFP components import from src.document_preprocessing, src.chunking,
# src.removal.propagate_gcs_deletions, and src.utils at runtime inside the container.
COPY src/ src/

# .env.example, not .env: .env is gitignored and therefore absent from the
# Cloud Build context. It supplies non-secret defaults; real values arrive as
# environment variables, which win because every loader uses override=False.
# via python-dotenv (e.g. model names, embedding dimensions).
COPY .env.example .

# Copy the KFP pipeline package (DAG definition + component wrappers).
# Note: this is only needed if running submit_pipeline.py from inside
# the container. For normal Vertex AI execution, KFP serializes the
# component function body and sends it to the container directly.
COPY data_ingestion_pipeline/data_ingestion_pipeline/ data_ingestion_pipeline/

# Set PYTHONPATH so that `from src.xxx` and `from data_ingestion_pipeline.xxx`
# resolve correctly regardless of the working directory.
ENV PYTHONPATH=/app
