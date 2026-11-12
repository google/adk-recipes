#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -e

_ENV_GCP_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"

# Load local .env if present
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Configuration (Parameterized via .env or environment variables)
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$_ENV_GCP_PROJECT}"
if [ -z "$PROJECT_ID" ]; then
  echo "[ERROR] GOOGLE_CLOUD_PROJECT must be set via environment or .env." >&2
  exit 1
fi
if [ -n "${CLOUD_RUN_REGION:-}" ] && [ "${CLOUD_RUN_REGION}" != "global" ]; then
  REGION="$CLOUD_RUN_REGION"
elif [ -n "${GOOGLE_CLOUD_LOCATION:-}" ] && [ "${GOOGLE_CLOUD_LOCATION}" != "global" ]; then
  REGION="$GOOGLE_CLOUD_LOCATION"
else
  REGION="us-central1"
fi
VERTEX_LOCATION="${GOOGLE_CLOUD_LOCATION:-$REGION}"
SERVICE_NAME="${SERVICE_NAME:-adk-token-tower}"
DATASET_ID="${BQ_ANALYTICS_DATASET_ID:-bq_adk_ds}"
TABLE_ID="${BQ_ANALYTICS_TABLE_ID:-adk_agent_events}"
VIEW_ID="${BQ_ANALYTICS_VIEW_ID:-v_llm_response}"
ALLOWED_HOSTS="${TOKENOMICS_ALLOWED_HOSTS:-*.run.app}"
CONTAINER_PORT="${PORT:-8080}"

echo "==================================================="
echo "🚀 Deploying $SERVICE_NAME to Google Cloud Run..."
echo "Project : $PROJECT_ID"
echo "Region  : $REGION"
echo "Dataset : $DATASET_ID.$TABLE_ID (View: $VIEW_ID)"
echo "==================================================="

ALL_ENV_CONCAT="${PROJECT_ID}${VERTEX_LOCATION}${REGION}${DATASET_ID}${TABLE_ID}${VIEW_ID}${ALLOWED_HOSTS}"
ENV_DELIM=""
for delim in '@' '|' '~' '^' '%'; do
  case "$ALL_ENV_CONCAT" in
    *"$delim"*) ;;
    *)
      if [ "$delim" != "^" ]; then
        ENV_DELIM="$delim"
        break
      fi
      ;;
  esac
done
if [ -z "$ENV_DELIM" ]; then
  for delim in ';;' '::' '##' '@@' '||'; do
    case "$ALL_ENV_CONCAT" in
      *"$delim"*) ;;
      *)
        ENV_DELIM="$delim"
        break
        ;;
    esac
  done
fi
if [ -z "$ENV_DELIM" ]; then
  echo "[ERROR] Could not select a collision-free gcloud --set-env-vars delimiter." >&2
  exit 1
fi
D="$ENV_DELIM"
SET_ENV_VARS="^${D}^GOOGLE_CLOUD_PROJECT=${PROJECT_ID}${D}GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}${D}CLOUD_RUN_REGION=${REGION}${D}GOOGLE_GENAI_USE_VERTEXAI=TRUE${D}BIGQUERY_PROJECT_ID=${PROJECT_ID}${D}BQ_ANALYTICS_DATASET_ID=${DATASET_ID}${D}BQ_ANALYTICS_TABLE_ID=${TABLE_ID}${D}BQ_ANALYTICS_VIEW_ID=${VIEW_ID}${D}TOKENOMICS_ALLOWED_HOSTS=${ALLOWED_HOSTS}"

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --port "$CONTAINER_PORT" \
  --no-allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --set-env-vars="$SET_ENV_VARS"

echo "==================================================="
echo "✅ Deployment Complete!"
echo "==================================================="
