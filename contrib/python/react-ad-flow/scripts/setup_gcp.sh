#!/bin/bash
# Copyright 2025 Google LLC
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
# GCP Setup Script for Ad Campaign Agent
# =============================================================================
# This script sets up all required GCP resources for deploying the ad campaign
# agent to Cloud Run with GCS storage.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Run: gcloud auth application-default login (before running this script)
#
# Usage:
#   ./scripts/setup_gcp.sh              # Full setup with asset upload
#   ./scripts/setup_gcp.sh --no-assets  # Setup without uploading assets
#   ./scripts/setup_gcp.sh --verify     # Verify existing setup
# =============================================================================

set -e  # Exit on error

# =============================================================================
# Configuration
# =============================================================================
GOOGLE_CLOUD_PROJECT="kaggle-on-gcp"
GOOGLE_CLOUD_LOCATION="us-central1"
GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-ad-campaign-assets"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory (for relative paths)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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
    echo -e "  $1"
}

# =============================================================================
# Parse Arguments
# =============================================================================
UPLOAD_ASSETS=true
VERIFY_ONLY=false

for arg in "$@"; do
    case $arg in
        --no-assets)
            UPLOAD_ASSETS=false
            shift
            ;;
        --verify)
            VERIFY_ONLY=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-assets    Skip uploading seed images and videos"
            echo "  --verify       Only verify existing setup, don't create resources"
            echo "  --help, -h     Show this help message"
            exit 0
            ;;
    esac
done

# =============================================================================
# Verify Mode
# =============================================================================
if [ "$VERIFY_ONLY" = true ]; then
    print_header "Verifying GCP Setup"

    echo ""
    echo "Configuration:"
    print_info "Project:  $GOOGLE_CLOUD_PROJECT"
    print_info "Location: $GOOGLE_CLOUD_LOCATION"
    print_info "Bucket:   $GCS_BUCKET"
    echo ""

    # Check project
    echo "Checking project..."
    if gcloud projects describe "$GOOGLE_CLOUD_PROJECT" &>/dev/null; then
        print_success "Project exists: $GOOGLE_CLOUD_PROJECT"
    else
        print_error "Project not found: $GOOGLE_CLOUD_PROJECT"
    fi

    # Check bucket
    echo "Checking bucket..."
    if gcloud storage buckets describe "gs://$GCS_BUCKET" &>/dev/null; then
        print_success "Bucket exists: $GCS_BUCKET"

        # Count assets
        IMAGE_COUNT=$(gcloud storage ls "gs://$GCS_BUCKET/seed-images/" 2>/dev/null | wc -l | tr -d ' ')
        VIDEO_COUNT=$(gcloud storage ls "gs://$GCS_BUCKET/generated/*.mp4" 2>/dev/null | wc -l | tr -d ' ')
        print_info "  Seed images: $IMAGE_COUNT"
        print_info "  Videos: $VIDEO_COUNT"
    else
        print_error "Bucket not found: $GCS_BUCKET"
    fi

    # Check APIs
    echo "Checking APIs..."
    for api in run.googleapis.com storage.googleapis.com aiplatform.googleapis.com; do
        if gcloud services list --enabled --filter="name:$api" --format="value(name)" 2>/dev/null | grep -q "$api"; then
            print_success "API enabled: $api"
        else
            print_warning "API not enabled: $api"
        fi
    done

    exit 0
fi

# =============================================================================
# Main Setup
# =============================================================================
print_header "GCP Setup for Ad Campaign Agent"

echo ""
echo "Configuration:"
print_info "Project:  $GOOGLE_CLOUD_PROJECT"
print_info "Location: $GOOGLE_CLOUD_LOCATION"
print_info "Bucket:   $GCS_BUCKET"
print_info "Upload assets: $UPLOAD_ASSETS"
echo ""

# Confirm before proceeding
read -p "Proceed with setup? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Setup cancelled."
    exit 0
fi

# =============================================================================
# Step 1: Set Project
# =============================================================================
print_header "Step 1: Setting Active Project"

gcloud config set project "$GOOGLE_CLOUD_PROJECT"
print_success "Project set to: $GOOGLE_CLOUD_PROJECT"

# =============================================================================
# Step 2: Enable APIs
# =============================================================================
print_header "Step 2: Enabling Required APIs"

APIS=(
    "run.googleapis.com"
    "storage.googleapis.com"
    "aiplatform.googleapis.com"
    "generativelanguage.googleapis.com"
)

for api in "${APIS[@]}"; do
    echo "Enabling $api..."
    gcloud services enable "$api" --quiet
    print_success "Enabled: $api"
done

# =============================================================================
# Step 3: Create GCS Bucket
# =============================================================================
print_header "Step 3: Creating GCS Bucket"

if gcloud storage buckets describe "gs://$GCS_BUCKET" &>/dev/null; then
    print_warning "Bucket already exists: $GCS_BUCKET"
else
    gcloud storage buckets create "gs://$GCS_BUCKET" \
        --project="$GOOGLE_CLOUD_PROJECT" \
        --location="$GOOGLE_CLOUD_LOCATION" \
        --uniform-bucket-level-access
    print_success "Bucket created: $GCS_BUCKET"
fi

# =============================================================================
# Step 4: Upload Assets (if enabled)
# =============================================================================
if [ "$UPLOAD_ASSETS" = true ]; then
    print_header "Step 4: Uploading Assets to GCS"

    # Upload seed images
    echo "Uploading seed images..."
    cd "$PROJECT_DIR"

    if [ -d "selected" ] && [ "$(ls -A selected/*.jpg selected/*.png 2>/dev/null)" ]; then
        # Upload JPGs
        if ls selected/*.jpg &>/dev/null; then
            gcloud storage cp selected/*.jpg "gs://$GCS_BUCKET/seed-images/" --quiet
            JPG_COUNT=$(ls selected/*.jpg 2>/dev/null | wc -l | tr -d ' ')
            print_success "Uploaded $JPG_COUNT JPG images"
        fi

        # Upload PNGs
        if ls selected/*.png &>/dev/null; then
            gcloud storage cp selected/*.png "gs://$GCS_BUCKET/seed-images/" --quiet
            PNG_COUNT=$(ls selected/*.png 2>/dev/null | wc -l | tr -d ' ')
            print_success "Uploaded $PNG_COUNT PNG images"
        fi
    else
        print_warning "No images found in selected/ directory"
    fi

    # Upload generated videos
    echo "Uploading generated videos..."
    if [ -d "generated" ] && ls generated/*.mp4 &>/dev/null; then
        gcloud storage cp generated/*.mp4 "gs://$GCS_BUCKET/generated/" --quiet
        VIDEO_COUNT=$(ls generated/*.mp4 2>/dev/null | wc -l | tr -d ' ')
        print_success "Uploaded $VIDEO_COUNT videos"
    else
        print_warning "No videos found in generated/ directory"
    fi
else
    print_header "Step 4: Skipping Asset Upload (--no-assets)"
    print_info "Run without --no-assets to upload seed images and videos"
fi

# =============================================================================
# Step 5: Verify Setup
# =============================================================================
print_header "Step 5: Verifying Setup"

echo "Listing bucket contents..."
echo ""
echo "Seed Images:"
gcloud storage ls "gs://$GCS_BUCKET/seed-images/" 2>/dev/null || echo "  (empty)"
echo ""
echo "Generated Videos:"
gcloud storage ls "gs://$GCS_BUCKET/generated/" 2>/dev/null || echo "  (empty)"

# =============================================================================
# Summary
# =============================================================================
print_header "Setup Complete!"

echo ""
echo "Next Steps:"
echo ""
echo "1. Test GCS integration locally:"
echo -e "   ${YELLOW}export GCS_BUCKET=\"$GCS_BUCKET\"${NC}"
echo -e "   ${YELLOW}python scripts/test_gcs.py${NC}"
echo ""
echo "2. Run the agent with GCS mode:"
echo -e "   ${YELLOW}export GCS_BUCKET=\"$GCS_BUCKET\"${NC}"
echo -e "   ${YELLOW}export GOOGLE_GENAI_USE_VERTEXAI=\"True\"${NC}"
echo -e "   ${YELLOW}adk web .${NC}"
echo ""
echo "3. Deploy to Cloud Run:"
echo -e "   ${YELLOW}./scripts/deploy.sh${NC}"
echo ""
echo "Environment variables for your shell (add to .bashrc/.zshrc):"
echo -e "   ${BLUE}export GOOGLE_CLOUD_PROJECT=\"$GOOGLE_CLOUD_PROJECT\"${NC}"
echo -e "   ${BLUE}export GCS_BUCKET=\"$GCS_BUCKET\"${NC}"
echo ""
