#!/usr/bin/env bash
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

#
# Deploys the AQA dashboard (`ui/`) to Cloud Run. `agents-cli deploy` handles
# this during full deployments (see extension/acli_ui.py); use this script when
# managing the service independently or testing the container image directly.
#
# The UI is a standalone FastAPI service that talks to the deployed AQA agent
# over its command and A2A routes. It shows two lists --
# investigations and insights -- and one detail panel holding the effective
# configuration, one investigation or one insight. Every action it takes is a
# deterministic protocol command (see src/ambient_quality_shared/protocol.py),
# so the service needs nothing beyond the ability to call the engine.
#
# What this script does:
#   1. (optional) create a dedicated runtime service account for the service
#   2. grant it roles/aiplatform.user on the ENGINE project, to call the engine
#   3. stage the UI and the shared wire client into one build context
#   4. deploy the service from that context (uses ui/Dockerfile)
#   5. print the service URL
#
# Usage:
#   ui/deploy_ui_cloud_run.sh
#
# Configure via env vars (defaults target the current AQA deployment):
#   PROJECT              GCP project to host the Cloud Run service
#   REGION               Cloud Run region                    (default us-central1)
#   SERVICE              Cloud Run service name               (default aqa-ui)
#   AGENT_ENGINE_RESOURCE_ID  full reasoningEngine resource name (required)
#   ENGINE_PROJECT       project that owns the engine (for the aiplatform.user
#                        grant); parsed from the resource name if unset
#   SERVICE_ACCOUNT      runtime SA email; created if it's the default name
#   ALLOW_UNAUTH         "true" to make the service public    (default false)
#
set -euo pipefail

# --- defaults (match the current AQA engine) ------------------------------- #
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PROJECT_ID="$(gcloud projects describe ${PROJECT} --format='value(projectNumber)')"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
# ENGINE_ID must point to the AQA agent
DEFAULT_ENGINE="projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines/${ENGINE_ID}"
SERVICE="${SERVICE:-aqua-ui}"
AGENT_ENGINE_RESOURCE_ID="${AGENT_ENGINE_RESOURCE_ID:-$DEFAULT_ENGINE}"
ALLOW_UNAUTH="${ALLOW_UNAUTH:-false}"

# Repository paths, resolved from this script's location in ui/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="${REPO_ROOT}/ui"
SHARED_SRC="${REPO_ROOT}/src/ambient_quality_shared"

# Engine project (for the aiplatform.user grant): the project segment of the
# resource name (a project NUMBER is fine for IAM bindings).
if [[ -z "${ENGINE_PROJECT:-}" ]]; then
  ENGINE_PROJECT="$(printf '%s' "$AGENT_ENGINE_RESOURCE_ID" | sed -E 's#projects/([^/]+)/.*#\1#')"
fi

# Runtime service account (dedicated by default).
SA_NAME="aqa-ui-sa"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${SA_NAME}@${PROJECT}.iam.gserviceaccount.com}"

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: set PROJECT (or 'gcloud config set project ...')." >&2; exit 1
fi
if [[ ! -d "$UI_DIR" ]]; then
  echo "ERROR: UI not found at $UI_DIR" >&2; exit 1
fi
if [[ ! -d "$SHARED_SRC" ]]; then
  echo "ERROR: shared client not found at $SHARED_SRC" >&2; exit 1
fi

echo "Project:        $PROJECT"
echo "Region:         $REGION"
echo "Service:        $SERVICE"
echo "Engine:         $AGENT_ENGINE_RESOURCE_ID"
echo "Engine project: $ENGINE_PROJECT"
echo "Runtime SA:     $SERVICE_ACCOUNT"
echo "Public:         $ALLOW_UNAUTH"
echo

# --- 0. enable required APIs (idempotent) ---------------------------------- #
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com --project="$PROJECT"

# --- 1. runtime service account (create only the dedicated default) -------- #
if [[ "$SERVICE_ACCOUNT" == "${SA_NAME}@${PROJECT}.iam.gserviceaccount.com" ]]; then
  gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
      --display-name="AQA chat UI (Cloud Run) runtime"
fi

# --- 2. IAM: call the engine ---------------------------------------------- #
gcloud projects add-iam-policy-binding "$ENGINE_PROJECT" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/aiplatform.user" --condition=None >/dev/null

# --- 3. stage the build context -------------------------------------------- #
#
# Two directories become one build context:
#
#   ui/                          the app, its front ends, and how to build them
#   src/ambient_quality_shared/  the wire client, shared with the CLI
#
# The shared package is a sibling rather than a subdirectory, so it cannot reach
# the builder through `--source="$UI_DIR"`. Pointing `--source` at the repo root
# instead is not an option: the root .gcloudignore excludes /ui/ from the
# agent's image, so the UI would exclude itself from its own build.
#
# The copies are plain `cp -R` of working directories, which can hold a local
# .venv, __pycache__, an installed web/node_modules and a built static_v2 from
# running the UI by hand. The UI's .dockerignore and .gcloudignore (copied in
# with it) keep all four out of the image and out of the upload either way;
# dropping them here just avoids shifting them around first -- which for
# node_modules alone is ~290 MB copied to a temp directory and deleted again.
#
# The image builds the front end itself, in the Dockerfile's Node stage, so a
# locally built bundle is not merely redundant: it is whatever the developer
# last happened to compile.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -R "${UI_DIR}/." "$STAGE/"
cp -R "$SHARED_SRC" "$STAGE/"
rm -rf "${STAGE:?}/.venv" "${STAGE:?}/web/node_modules" "${STAGE:?}/static_v2"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- 4. deploy from the staged context (uses ui/Dockerfile) ---------------- #
AUTH_FLAG="--no-allow-unauthenticated"
[[ "$ALLOW_UNAUTH" == "true" ]] && AUTH_FLAG="--allow-unauthenticated"

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --source="$STAGE" \
  --service-account="$SERVICE_ACCOUNT" \
  --set-env-vars="AGENT_ENGINE_RESOURCE_ID=${AGENT_ENGINE_RESOURCE_ID},GOOGLE_API_USE_CLIENT_CERTIFICATE=false" \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=3 \
  --port=8080 \
  --labels=component=aqa \
  $AUTH_FLAG

URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
echo
echo "Deployed: $URL"
if [[ "$ALLOW_UNAUTH" != "true" ]]; then
  echo "Service requires auth. For a quick authenticated check:"
  echo "  gcloud run services proxy $SERVICE --region=$REGION --project=$PROJECT"
fi
