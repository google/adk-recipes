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

# =============================================================================
# Deploy Script for Ad Campaign Agent to Cloud Run
# =============================================================================
# This script deploys the ad campaign agent to Google Cloud Run with the
# ADK Web UI enabled and public access configured.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - adk CLI installed (uv tool install google-adk)
#   - GCS bucket created with assets (run setup_gcp.sh first)
#
# Usage:
#   ./scripts/deploy.sh                    # Deploy with defaults (private access)
#   ./scripts/deploy.sh --public           # Deploy with public access
#   ./scripts/deploy.sh --private          # Deploy without public access (default)
#   ./scripts/deploy.sh --dry-run          # Show commands without executing
# =============================================================================

set -e  # Exit on error

# Script directory (for relative paths)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/app"

# Load environment variables from .env if present
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$PROJECT_DIR/.env"
    set +a
fi

# =============================================================================
# Configuration
# =============================================================================
GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"  # Cloud Run deployment region
VERTEX_AI_LOCATION="${VERTEX_AI_LOCATION:-global}"     # Vertex AI API location (for Gemini 3.x models)
GCS_BUCKET="${GCS_BUCKET:-${GOOGLE_CLOUD_PROJECT}-react-ad-flow-assets}"
SERVICE_NAME="${SERVICE_NAME:-react-ad-flow}"
APP_NAME="${APP_NAME:-react_ad_flow}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# =============================================================================
# Helper Functions
# =============================================================================
print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${CYAN}  $1${NC}"
}

# =============================================================================
# Parse Arguments
# =============================================================================
ALLOW_UNAUTHENTICATED=false
DRY_RUN=false
ENABLE_TRACE=false

for arg in "$@"; do
    case $arg in
        --public)
            ALLOW_UNAUTHENTICATED=true
            shift
            ;;
        --private)
            ALLOW_UNAUTHENTICATED=false
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --trace)
            ENABLE_TRACE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --public      Deploy with public unauthenticated access"
            echo "  --private     Deploy without public access (requires authentication, default)"
            echo "  --dry-run     Show deployment command without executing"
            echo "  --trace       Enable Cloud Trace for observability"
            echo "  --help, -h    Show this help message"
            echo ""
            echo "Environment Variables (optional overrides):"
            echo "  GOOGLE_CLOUD_PROJECT   GCP project ID"
            echo "  CLOUD_RUN_REGION       Cloud Run region (default: us-central1)"
            echo "  VERTEX_AI_LOCATION     Vertex AI location (default: global)"
            echo "  GCS_BUCKET             GCS bucket name"
            echo "  SERVICE_NAME           Cloud Run service name (default: react-ad-flow)"
            exit 0
            ;;
    esac
done

if [ -z "$GOOGLE_CLOUD_PROJECT" ]; then
    print_error "GOOGLE_CLOUD_PROJECT is not set. Please set it in .env or via environment variable."
    exit 1
fi

# =============================================================================
# Pre-flight Checks
# =============================================================================
print_header "Pre-flight Checks"

# Check gcloud
if ! command -v gcloud &> /dev/null; then
    print_error "gcloud CLI not found. Please install Google Cloud SDK."
    exit 1
fi
print_success "gcloud CLI found"

# Check adk
if ! command -v adk &> /dev/null; then
    print_error "adk CLI not found. Please install: uv tool install google-adk"
    exit 1
fi
print_success "adk CLI found"

# Check agent directory
if [ ! -d "$AGENT_DIR" ]; then
    print_error "Agent directory not found: $AGENT_DIR"
    exit 1
fi
print_success "Agent directory found: $AGENT_DIR"

# Check if authenticated
if ! gcloud auth print-identity-token &> /dev/null; then
    print_warning "Not authenticated with gcloud. Running: gcloud auth login"
    gcloud auth login
fi
print_success "gcloud authenticated"

# =============================================================================
# Configuration Summary
# =============================================================================
print_header "Deployment Configuration"

echo ""
echo "Configuration:"
print_info "Project:        $GOOGLE_CLOUD_PROJECT"
print_info "Cloud Run Region: $CLOUD_RUN_REGION"
print_info "Vertex AI Location: $VERTEX_AI_LOCATION"
print_info "Service Name:   $SERVICE_NAME"
print_info "App Name:       $APP_NAME"
print_info "GCS Bucket:     $GCS_BUCKET"
print_info "Agent Path:     $AGENT_DIR"
print_info "Public Access:  $ALLOW_UNAUTHENTICATED"
print_info "Cloud Trace:    $ENABLE_TRACE"
echo ""

# =============================================================================
# Build Deploy Command
# =============================================================================

# Base command
DEPLOY_CMD="adk deploy cloud_run"
DEPLOY_CMD="$DEPLOY_CMD --project=$GOOGLE_CLOUD_PROJECT"
DEPLOY_CMD="$DEPLOY_CMD --region=$CLOUD_RUN_REGION"
DEPLOY_CMD="$DEPLOY_CMD --service_name=$SERVICE_NAME"
DEPLOY_CMD="$DEPLOY_CMD --app_name=$APP_NAME"
DEPLOY_CMD="$DEPLOY_CMD --with_ui"

# Use GCS for artifact storage (avoids permission errors on Cloud Run)
DEPLOY_CMD="$DEPLOY_CMD --artifact_service_uri=gs://$GCS_BUCKET"

# Optional: Cloud Trace
if [ "$ENABLE_TRACE" = true ]; then
    DEPLOY_CMD="$DEPLOY_CMD --trace_to_cloud"
fi

# Agent path
DEPLOY_CMD="$DEPLOY_CMD $AGENT_DIR"

# gcloud pass-through arguments (after --)
GCLOUD_ARGS=""

# Environment variables for Cloud Run
GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=GCS_BUCKET=$GCS_BUCKET"
GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=True"
GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=GOOGLE_CLOUD_PROJECT=$GOOGLE_CLOUD_PROJECT"
GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=GOOGLE_CLOUD_LOCATION=$VERTEX_AI_LOCATION"
GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=APP_MODE=${APP_MODE:-demo}"

# Google Maps API Key (optional, for location features)
if [ -n "$MAPS_API_KEY" ]; then
    GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=MAPS_API_KEY=$MAPS_API_KEY"
    print_success "Maps API Key will be passed to Cloud Run"
elif [ -n "$GOOGLE_MAPS_API_KEY" ]; then
    GCLOUD_ARGS="$GCLOUD_ARGS --set-env-vars=GOOGLE_MAPS_API_KEY=$GOOGLE_MAPS_API_KEY"
    print_success "Google Maps API Key will be passed to Cloud Run"
else
    print_warning "No Maps API Key found. Location features will be limited."
    print_info "Set MAPS_API_KEY or GOOGLE_MAPS_API_KEY environment variable before deploying."
fi

# Memory and CPU for video processing
GCLOUD_ARGS="$GCLOUD_ARGS --memory=2Gi"
GCLOUD_ARGS="$GCLOUD_ARGS --cpu=2"
GCLOUD_ARGS="$GCLOUD_ARGS --timeout=600"

# Public access
if [ "$ALLOW_UNAUTHENTICATED" = true ]; then
    GCLOUD_ARGS="$GCLOUD_ARGS --allow-unauthenticated"
else
    GCLOUD_ARGS="$GCLOUD_ARGS --no-allow-unauthenticated"
fi

# Full command with gcloud args
FULL_CMD="$DEPLOY_CMD -- $GCLOUD_ARGS"

# =============================================================================
# Deploy
# =============================================================================
print_header "Deployment Command"

# Mask sensitive API keys when echoing command
DISPLAY_CMD="$FULL_CMD"
if [ -n "$MAPS_API_KEY" ]; then
    DISPLAY_CMD="${DISPLAY_CMD//MAPS_API_KEY=$MAPS_API_KEY/MAPS_API_KEY=[REDACTED]}"
fi
if [ -n "$GOOGLE_MAPS_API_KEY" ]; then
    DISPLAY_CMD="${DISPLAY_CMD//GOOGLE_MAPS_API_KEY=$GOOGLE_MAPS_API_KEY/GOOGLE_MAPS_API_KEY=[REDACTED]}"
fi

echo ""
echo "Command to execute:"
echo -e "${YELLOW}$DISPLAY_CMD${NC}"
echo ""

if [ "$DRY_RUN" = true ]; then
    print_warning "Dry run mode - not executing"
    echo ""
    echo "To deploy, run without --dry-run:"
    echo "  ./scripts/deploy.sh"
    exit 0
fi

# Confirm before deploying
read -p "Proceed with deployment? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled."
    exit 0
fi

print_header "Deploying to Cloud Run"

# Change to project directory and deploy
cd "$PROJECT_DIR"
eval $FULL_CMD

# =============================================================================
# Post-Deployment
# =============================================================================
print_header "Deployment Complete!"

# Get the service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
    --project=$GOOGLE_CLOUD_PROJECT \
    --region=$CLOUD_RUN_REGION \
    --format='value(status.url)' 2>/dev/null || echo "")

if [ -n "$SERVICE_URL" ]; then
    echo ""
    echo -e "${GREEN}Service URL:${NC}"
    echo -e "  ${CYAN}$SERVICE_URL${NC}"
    echo ""
    echo -e "${GREEN}ADK Web UI:${NC}"
    echo -e "  ${CYAN}$SERVICE_URL/dev-ui${NC}"
    echo ""

    if [ "$ALLOW_UNAUTHENTICATED" = true ]; then
        echo "The service is publicly accessible. Open the Web UI URL in your browser."
    else
        echo "The service requires authentication. To access:"
        echo "  1. Get a token: export TOKEN=\$(gcloud auth print-identity-token)"
        echo "  2. Use in requests: curl -H \"Authorization: Bearer \$TOKEN\" $SERVICE_URL"
    fi
else
    print_warning "Could not retrieve service URL. Check Cloud Run console."
fi

echo ""
echo "Useful commands:"
echo "  # View logs"
echo "  gcloud run services logs read $SERVICE_NAME --project=$GOOGLE_CLOUD_PROJECT --region=$CLOUD_RUN_REGION"
echo ""
echo "  # Update service"
echo "  ./scripts/deploy.sh"
echo ""
echo "  # Delete service"
echo "  gcloud run services delete $SERVICE_NAME --project=$GOOGLE_CLOUD_PROJECT --region=$CLOUD_RUN_REGION"
echo ""
