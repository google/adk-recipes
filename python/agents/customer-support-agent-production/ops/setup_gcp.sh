#!/bin/bash
# ============================================================================
# GCP Prerequisites Setup Script
# ============================================================================
# This script enables required APIs and configures IAM permissions
# for the Customer Support Multi-Agent System
#
# Usage:
#   ./ops/setup_gcp.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - GCP project created
#   - Billing enabled on project
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# Configuration
# ============================================================================

# Load from .env if exists
if [ -f .env ]; then
    echo -e "${BLUE}Loading configuration from .env...${NC}"
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Get project ID
PROJECT_ID=${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}

if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}Error: No GCP project ID found${NC}"
    echo "Please set GOOGLE_CLOUD_PROJECT in .env or run: gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

LOCATION=${GOOGLE_CLOUD_LOCATION:-us-central1}
BUCKET_NAME=${GOOGLE_CLOUD_STORAGE_BUCKET}

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  GCP Prerequisites Setup for Multi-Agent System           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Project ID:${NC} $PROJECT_ID"
echo -e "${BLUE}Location:${NC} $LOCATION"
echo ""

# ============================================================================
# 1. Enable Required APIs
# ============================================================================

echo -e "${YELLOW}[1/6] Enabling Required APIs...${NC}"
echo ""

APIS=(
    "aiplatform.googleapis.com"           # Vertex AI (Agent Engine, Gemini)
    "firestore.googleapis.com"            # Firestore database
    "run.googleapis.com"                  # Cloud Run
    "cloudbuild.googleapis.com"           # Cloud Build (for deployment)
    "storage.googleapis.com"              # Cloud Storage
    "artifactregistry.googleapis.com"     # Artifact Registry (container images)
    "cloudresourcemanager.googleapis.com" # Resource Manager
    "iam.googleapis.com"                  # IAM
    "logging.googleapis.com"              # Cloud Logging
    "monitoring.googleapis.com"           # Cloud Monitoring
    "cloudtrace.googleapis.com"           # Tracing for Agent Engine (LoggingPlugin)
    "modelarmor.googleapis.com"           # Model Armor (prompt safety screening)
    "dlp.googleapis.com"                  # Cloud DLP (used by Model Armor for PII detection)
)

for api in "${APIS[@]}"; do
    echo -e "  Enabling ${BLUE}$api${NC}..."
    if gcloud services enable "$api" --project="$PROJECT_ID" &>/dev/null; then
        echo -e "    ${GREEN}✓${NC} Enabled"
    else
        echo -e "    ${YELLOW}⚠${NC} Already enabled or error"
    fi
done

echo ""

# ============================================================================
# 2. Create Service Account (COMMENTED OUT - using default compute SA instead)
# ============================================================================
# NOTE: We use the default Compute Engine service account for Cloud Run
# instead of a custom service account. This simplifies the setup.
# Uncomment this section if you need a dedicated service account.

# echo -e "${YELLOW}[2/5] Creating Service Account...${NC}"
# echo ""
#
# SERVICE_ACCOUNT_NAME="customer-support-agent"
# SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
#
# if gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
#     echo -e "  ${YELLOW}⚠${NC} Service account already exists: $SERVICE_ACCOUNT_EMAIL"
# else
#     echo -e "  Creating service account: ${BLUE}$SERVICE_ACCOUNT_EMAIL${NC}"
#     gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
#         --display-name="Customer Support Agent" \
#         --description="Service account for multi-agent customer support system" \
#         --project="$PROJECT_ID"
#     echo -e "  ${GREEN}✓${NC} Service account created"
# fi
#
# echo ""

# ============================================================================
# 3. Grant IAM Roles to Custom Service Account (COMMENTED OUT)
# ============================================================================
# NOTE: Commented out since we're using the default compute SA.
# Uncomment if using a custom service account.

# echo -e "${YELLOW}[3/5] Granting IAM Roles to Service Account...${NC}"
# echo ""
#
# ROLES=(
#     "roles/aiplatform.user"              # Vertex AI access
#     "roles/aiplatform.serviceAgent"      # Vertex AI service operations
#     "roles/datastore.user"               # Firestore read/write
#     "roles/storage.objectAdmin"          # GCS bucket access
#     "roles/logging.logWriter"            # Write logs
#     "roles/run.invoker"                  # Invoke Cloud Run services
# )
#
# for role in "${ROLES[@]}"; do
#     echo -e "  Granting ${BLUE}$role${NC}..."
#     if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
#         --member="serviceAccount:$SERVICE_ACCOUNT_EMAIL" \
#         --role="$role" \
#         --condition=None \
#         --quiet &>/dev/null; then
#         echo -e "    ${GREEN}✓${NC} Granted"
#     else
#         echo -e "    ${YELLOW}⚠${NC} Already granted or error"
#     fi
# done
#
# echo ""

# ============================================================================
# 2. Grant IAM Roles to Agent Engine Service Account
# ============================================================================

echo -e "${YELLOW}[2/6] Granting IAM Roles to Agent Engine Service Account...${NC}"
echo ""

# Get numeric project ID
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

# Two Vertex AI service agents need Firestore access:
#   gcp-sa-aiplatform-re  — runs your agent/tool code at runtime (created on first Agent Engine deploy)
#   gcp-sa-aiplatform     — general Vertex AI platform operations
# NOTE: If you run this script before your first Agent Engine deployment, the -re SA may not
# exist yet. Re-run `make setup-gcp` after your first `make deploy-agent-engine`.
AGENT_ENGINE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
VERTEX_AI_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"

echo -e "  Agent Engine SA: ${BLUE}$AGENT_ENGINE_SA${NC}"
echo -e "  Vertex AI SA:    ${BLUE}$VERTEX_AI_SA${NC}"

AGENT_ENGINE_ROLES=(
    "roles/datastore.user"               # Firestore read/write (required for tool calls)
    "roles/aiplatform.user"              # Vertex AI access
    "roles/storage.objectViewer"         # Read from GCS
)

for sa in "$AGENT_ENGINE_SA" "$VERTEX_AI_SA"; do
    for role in "${AGENT_ENGINE_ROLES[@]}"; do
        echo -e "  Granting ${BLUE}$role${NC} to ${BLUE}$sa${NC}..."
        if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:$sa" \
            --role="$role" \
            --condition=None \
            --quiet &>/dev/null; then
            echo -e "    ${GREEN}✓${NC} Granted"
        else
            echo -e "    ${YELLOW}⚠${NC} Already granted or error"
        fi
    done
done

echo ""

# ============================================================================
# 3. Grant IAM Roles to Cloud Run Default Compute Service Account
# ============================================================================

echo -e "${YELLOW}[3/6] Granting IAM Roles to Cloud Run Service Account...${NC}"
echo ""

CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo -e "  Cloud Run SA: ${BLUE}$CLOUD_RUN_SA${NC}"

CLOUD_RUN_ROLES=(
    "roles/aiplatform.user"              # Access Vertex AI Agent Engine
    "roles/datastore.user"               # Access Firestore database
    "roles/modelarmor.user"              # Call Model Armor API (prompt screening)
)

for role in "${CLOUD_RUN_ROLES[@]}"; do
    echo -e "  Granting ${BLUE}$role${NC}..."
    if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$CLOUD_RUN_SA" \
        --role="$role" \
        --condition=None \
        --quiet &>/dev/null; then
        echo -e "    ${GREEN}✓${NC} Granted"
    else
        echo -e "    ${YELLOW}⚠${NC} Already granted or error"
    fi
done

echo ""

# ============================================================================
# 4. Grant Permissions to Current User
# ============================================================================

echo -e "${YELLOW}[4/6] Granting Permissions to Current User...${NC}"
echo ""

CURRENT_USER=$(gcloud config get-value account 2>/dev/null)

if [ -z "$CURRENT_USER" ]; then
    echo -e "  ${RED}✗${NC} No authenticated user found. Run: gcloud auth login"
else
    echo -e "  Current user: ${BLUE}$CURRENT_USER${NC}"

    USER_ROLES=(
        "roles/aiplatform.admin"            # Deploy to Agent Engine
        "roles/datastore.owner"             # Firestore admin
        "roles/storage.admin"               # GCS admin
        "roles/run.admin"                   # Cloud Run admin
        "roles/iam.serviceAccountUser"      # Use service accounts
    )

    for role in "${USER_ROLES[@]}"; do
        echo -e "  Granting ${BLUE}$role${NC}..."
        if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="user:$CURRENT_USER" \
            --role="$role" \
            --condition=None \
            --quiet &>/dev/null; then
            echo -e "    ${GREEN}✓${NC} Granted"
        else
            echo -e "    ${YELLOW}⚠${NC} Already granted or error"
        fi
    done
fi

echo ""

# ============================================================================
# 5. Create GCS Bucket (if needed)
# ============================================================================

echo -e "${YELLOW}[5/6] Setting up GCS Bucket...${NC}"
echo ""

if [ -z "$BUCKET_NAME" ]; then
    echo -e "  ${YELLOW}⚠${NC} No GOOGLE_CLOUD_STORAGE_BUCKET set in .env"
    BUCKET_NAME="${PROJECT_ID}-staging"
    echo -e "  Using default: ${BLUE}$BUCKET_NAME${NC}"
fi

# Remove gs:// prefix if present
BUCKET_NAME=${BUCKET_NAME#gs://}

if gsutil ls -b "gs://$BUCKET_NAME" &>/dev/null; then
    echo -e "  ${YELLOW}⚠${NC} Bucket already exists: gs://$BUCKET_NAME"
else
    echo -e "  Creating bucket: ${BLUE}gs://$BUCKET_NAME${NC}"
    gsutil mb -p "$PROJECT_ID" -l "$LOCATION" "gs://$BUCKET_NAME"
    echo -e "  ${GREEN}✓${NC} Bucket created"
fi

# Grant Cloud Run service account access to bucket
echo -e "  Granting bucket access to Cloud Run service account..."
gsutil iam ch "serviceAccount:$CLOUD_RUN_SA:roles/storage.objectAdmin" "gs://$BUCKET_NAME" 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} Bucket permissions configured"

echo ""

# ============================================================================
# 6. Model Armor Setup (if MODEL_ARMOR_ENABLED=true in .env)
# ============================================================================

echo -e "${YELLOW}[6/6] Model Armor Setup...${NC}"
echo ""

MODEL_ARMOR_ENABLED=${MODEL_ARMOR_ENABLED:-false}

if [ "$MODEL_ARMOR_ENABLED" = "true" ]; then
    echo -e "  MODEL_ARMOR_ENABLED=true — running Model Armor setup..."
    echo ""
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if bash "$SCRIPT_DIR/setup_model_armor.sh"; then
        echo -e "  ${GREEN}✓${NC} Model Armor setup complete"
    else
        echo -e "  ${YELLOW}⚠${NC} Model Armor setup encountered errors — check output above"
    fi
else
    echo -e "  ${YELLOW}⚠${NC} MODEL_ARMOR_ENABLED is not 'true' in .env — skipping Model Armor setup"
    echo -e "     To enable: set MODEL_ARMOR_ENABLED=true in .env and re-run setup"
fi

echo ""

# ============================================================================
# Summary
# ============================================================================

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Setup Complete!                                           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo -e "  Project ID:         ${GREEN}$PROJECT_ID${NC}"
echo -e "  Location:           ${GREEN}$LOCATION${NC}"
echo -e "  Agent Engine SA:    ${GREEN}$AGENT_ENGINE_SA${NC}"
echo -e "  Cloud Run SA:       ${GREEN}$CLOUD_RUN_SA${NC}"
echo -e "  Storage Bucket:     ${GREEN}gs://$BUCKET_NAME${NC}"
echo ""
echo -e "${BLUE}Next Steps:${NC}"
echo -e "  1. Update .env file with bucket name:"
echo -e "     ${YELLOW}GOOGLE_CLOUD_STORAGE_BUCKET=$BUCKET_NAME${NC}"
echo ""
echo -e "  2. Create Firestore database:"
echo -e "     ${YELLOW}./ops/setup_firestore.sh${NC}"
echo ""
echo -e "  3. Deploy to Agent Engine:"
echo -e "     ${YELLOW}python deployment/deploy.py${NC}"
echo ""
echo -e "${GREEN}✓ All prerequisites configured successfully!${NC}"
echo ""
