#!/bin/bash
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
