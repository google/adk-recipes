#!/bin/bash
# Standalone Cloud Run deployment script fallback

set -e

PROJECT_ID="{{PROJECT_ID}}"
REGION="{{REGION}}"
SERVICE_NAME="vto-retail-app"

echo "=========================================================="
echo "DEPLOYING VIRTUAL TRY-ON APP TO GOOGLE CLOUD RUN"
echo "=========================================================="
echo "GCP Project:   $PROJECT_ID"
echo "Region:        $REGION"
echo "Service Name:  $SERVICE_NAME"
echo "=========================================================="

# 1. Enable APIs
echo "Enabling Cloud Run and Cloud Build APIs..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com --project="$PROJECT_ID" --quiet

# 2. Deploy directly from source (uses local Dockerfile)
echo "Deploying source container via Cloud Build and Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --quiet

echo "=========================================================="
echo "DEPLOYMENT COMPLETED SUCCESSFULLY"
echo "=========================================================="
