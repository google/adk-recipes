#!/bin/bash

set -e

# Determine which .env file to load based on ENV argument (default: .env)
ENV="${1:-}"
if [ -n "$ENV" ] && [ -f ".env.$ENV" ]; then
  ENV_FILE=".env.$ENV"
elif [ -f ".env" ]; then
  ENV_FILE=".env"
else
  ENV_FILE=""
fi

# Load configuration from env file
if [ -n "$ENV_FILE" ]; then
  echo "Loading configuration from $ENV_FILE..."
  while IFS='=' read -r key value; do
    # Skip blank lines and comment-only lines
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    # Strip inline comments and trailing whitespace from value
    value="${value%%#*}"
    value="${value%"${value##*[![:space:]]}"}"
    export "$key=$value"
  done < "$ENV_FILE"
fi

# Configuration - Use env vars or defaults
PROJECT_ID="${GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-customer-support-ai}"
REPOSITORY="${REPOSITORY:-customer-support-repo}"
IMAGE_NAME="${IMAGE_NAME:-customer-support-app}"
AGENT_ENGINE_RESOURCE_NAME="${AGENT_ENGINE_RESOURCE_NAME}"

# Validate required variables
if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT not set. Please set it in .env (repo root)"
  exit 1
fi

if [ -z "$AGENT_ENGINE_RESOURCE_NAME" ]; then
  echo "ERROR: AGENT_ENGINE_RESOURCE_NAME not set. Please set it in .env (repo root)"
  exit 1
fi

echo "================================================"
echo "Deploying Customer Support AI to Cloud Run"
echo "================================================"
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo "Service: $SERVICE_NAME"
echo "Agent Engine: $AGENT_ENGINE_RESOURCE_NAME"
echo ""

# Step 1: Enable required APIs
echo "Step 1: Enabling required APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  --project=$PROJECT_ID

# Step 2: Create Artifact Registry repository (if it doesn't exist)
echo ""
echo "Step 2: Creating Artifact Registry repository..."
if ! gcloud artifacts repositories describe $REPOSITORY \
  --location=$REGION \
  --project=$PROJECT_ID &>/dev/null; then
  gcloud artifacts repositories create $REPOSITORY \
    --repository-format=docker \
    --location=$REGION \
    --description="Customer Support AI images" \
    --project=$PROJECT_ID
  echo "Repository created."
else
  echo "Repository already exists."
fi

# Step 3: Build and push the Docker image
echo ""
echo "Step 3: Building and pushing Docker image..."
IMAGE_URL="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE_NAME"

# Configure Docker to use gcloud credentials
gcloud auth configure-docker $REGION-docker.pkg.dev --quiet

# Build the image
docker build -t $IMAGE_URL:latest -f backend/Dockerfile .

# Push the image
docker push $IMAGE_URL:latest

# Step 4: Deploy to Cloud Run
echo ""
echo "Step 4: Deploying to Cloud Run..."

# Build env vars string from the loaded env file — pass all relevant vars
CLOUD_RUN_ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
CLOUD_RUN_ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${REGION}"
CLOUD_RUN_ENV_VARS+=",AGENT_ENGINE_RESOURCE_NAME=${AGENT_ENGINE_RESOURCE_NAME}"
CLOUD_RUN_ENV_VARS+=",FRONTEND_URL=https://${SERVICE_NAME}-${REGION}.run.app"
CLOUD_RUN_ENV_VARS+=",FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-customer-support-db}"
CLOUD_RUN_ENV_VARS+=",MODEL_ARMOR_ENABLED=${MODEL_ARMOR_ENABLED:-false}"
CLOUD_RUN_ENV_VARS+=",MODEL_ARMOR_MODE=${MODEL_ARMOR_MODE:-INSPECT_AND_BLOCK}"
CLOUD_RUN_ENV_VARS+=",ENV=${ENV:-production}"
# Only include MODEL_ARMOR_TEMPLATE_ID if set
if [ -n "${MODEL_ARMOR_TEMPLATE_ID}" ]; then
  CLOUD_RUN_ENV_VARS+=",MODEL_ARMOR_TEMPLATE_ID=${MODEL_ARMOR_TEMPLATE_ID}"
fi

gcloud run deploy $SERVICE_NAME \
  --image=$IMAGE_URL:latest \
  --region=$REGION \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars="$CLOUD_RUN_ENV_VARS" \
  --memory=512Mi \
  --cpu=1 \
  --timeout=300 \
  --max-instances=10 \
  --project=$PROJECT_ID

# Get the service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
  --region=$REGION \
  --project=$PROJECT_ID \
  --format='value(status.url)')

echo ""
echo "================================================"
echo "Deployment Complete!"
echo "================================================"
echo "Service URL: $SERVICE_URL"
echo ""
echo "Test the deployment:"
echo "  curl $SERVICE_URL/health"
echo ""
echo "Access the frontend:"
echo "  $SERVICE_URL"
echo "================================================"
