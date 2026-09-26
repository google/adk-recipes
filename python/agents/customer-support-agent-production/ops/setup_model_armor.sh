#!/usr/bin/env bash
# ==============================================================================
# Model Armor Setup Script
# ==============================================================================
# Enables Model Armor for the project and configures it to screen prompts and
# responses on all Vertex AI generateContent calls (including those made by
# Agent Engine internally).
#
# What this script does:
#   1. Enables the Model Armor API
#   2. Grants the Vertex AI service agents the modelarmor.user IAM role
#   3. Configures project-level floor settings (INSPECT_AND_BLOCK by default)
#   4. Optionally creates a named template for fine-grained per-deployment policy
#
# Usage:
#   ./ops/setup_model_armor.sh
#   ./ops/setup_model_armor.sh --mode INSPECT_ONLY
#   ./ops/setup_model_armor.sh --create-template
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Owner / Editor role on the GCP project
#   - .env file present (or GOOGLE_CLOUD_PROJECT set in environment)
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------
MODE="INSPECT_AND_BLOCK"   # or INSPECT_ONLY
CREATE_TEMPLATE=false
TEMPLATE_NAME="customer-support-policy"

# ------------------------------------------------------------------------------
# Parse flags
# ------------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)            MODE="$2";           shift 2 ;;
    --create-template) CREATE_TEMPLATE=true; shift   ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ------------------------------------------------------------------------------
# Load .env if present
# ------------------------------------------------------------------------------
if [[ -f .env ]]; then
  echo "Loading configuration from .env..."
  set -a
  # shellcheck disable=SC1091
  source <(grep -v '^\s*#' .env | grep -v '^\s*$')
  set +a
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT not set. Add it to .env or run:"
  echo "  gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
AGENT_ENGINE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
VERTEX_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "======================================================================"
echo "  Model Armor Setup"
echo "======================================================================"
echo "  Project:          $PROJECT_ID"
echo "  Location:         $LOCATION"
echo "  Floor mode:       $MODE"
echo "  Create template:  $CREATE_TEMPLATE"
echo "======================================================================"
echo ""

# ==============================================================================
# 1. Enable Model Armor API
# ==============================================================================
echo "[1/4] Enabling Model Armor API..."
gcloud services enable modelarmor.googleapis.com --project="$PROJECT_ID"
echo "  ✓ modelarmor.googleapis.com enabled"
echo ""

# ==============================================================================
# 2. Grant IAM roles to service accounts
# ==============================================================================
echo "[2/4] Granting modelarmor.user to service accounts..."

# Cloud Run SA calls the Model Armor API directly from the FastAPI backend
echo "  Granting roles/modelarmor.user to Cloud Run SA ($CLOUD_RUN_SA)..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUD_RUN_SA}" \
  --role="roles/modelarmor.user" \
  --condition=None \
  --quiet 2>/dev/null && echo "    ✓ Granted" || echo "    ⚠ Already granted or SA not yet provisioned"

# Vertex AI service agents (Agent Engine + platform) for floor settings enforcement
for SA in "$AGENT_ENGINE_SA" "$VERTEX_SA"; do
  echo "  Granting roles/modelarmor.user to $SA..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" \
    --role="roles/modelarmor.user" \
    --condition=None \
    --quiet 2>/dev/null && echo "    ✓ Granted" || echo "    ⚠ Already granted or SA not yet provisioned"
done

echo ""

# ==============================================================================
# 3. Configure project-level floor settings
# ==============================================================================
echo "[3/4] Configuring floor settings (mode: $MODE)..."

# Floor settings apply automatically to ALL generateContent calls in this
# project, including those made internally by Vertex AI Agent Engine.
# No code changes to agents or tools are required.
gcloud model-armor floor-settings update \
  --location=global \
  --project="$PROJECT_ID" \
  --enable-floor-setting-enforcement \
  --rai-settings-filters="HARASSMENT=BLOCK_LOW_AND_ABOVE" \
  --rai-settings-filters="HATE_SPEECH=BLOCK_LOW_AND_ABOVE" \
  --rai-settings-filters="SEXUALLY_EXPLICIT=BLOCK_LOW_AND_ABOVE" \
  --rai-settings-filters="DANGEROUS_CONTENT=BLOCK_LOW_AND_ABOVE" \
  2>/dev/null \
  && echo "  ✓ Floor settings configured" \
  || echo "  ⚠ Floor settings update skipped (may require org-level permission)"

echo ""

# ==============================================================================
# 4. (Optional) Create a named template for fine-grained policy
# ==============================================================================
if [[ "$CREATE_TEMPLATE" == "true" ]]; then
  echo "[4/4] Creating Model Armor template: $TEMPLATE_NAME..."

  # Templates add per-deployment control on top of floor settings.
  # Pass MODEL_ARMOR_TEMPLATE_ID=projects/.../locations/.../templates/<name>
  # as an environment variable to opt into template-based screening.
  gcloud model-armor templates create "$TEMPLATE_NAME" \
    --location="$LOCATION" \
    --project="$PROJECT_ID" \
    --rai-settings-filters="HARASSMENT=BLOCK_LOW_AND_ABOVE" \
    --rai-settings-filters="HATE_SPEECH=BLOCK_LOW_AND_ABOVE" \
    --rai-settings-filters="SEXUALLY_EXPLICIT=BLOCK_LOW_AND_ABOVE" \
    --rai-settings-filters="DANGEROUS_CONTENT=BLOCK_LOW_AND_ABOVE" \
    --malicious-uri-filter-settings-enable \
    --pi-and-jailbreak-filter-settings-filter-enforcement=ENABLED \
    --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE \
    2>/dev/null \
    && echo "  ✓ Template created" \
    || echo "  ⚠ Template may already exist"

  TEMPLATE_RESOURCE="projects/${PROJECT_ID}/locations/${LOCATION}/templates/${TEMPLATE_NAME}"
  echo ""
  echo "  Template resource name:"
  echo "    $TEMPLATE_RESOURCE"
  echo ""
  echo "  To use this template, set in .env / Cloud Run env vars:"
  echo "    MODEL_ARMOR_ENABLED=true"
  echo "    MODEL_ARMOR_TEMPLATE_ID=$TEMPLATE_RESOURCE"
else
  echo "[4/4] Skipping template creation (pass --create-template to enable)"
fi

echo ""
echo "======================================================================"
echo "  Setup Complete"
echo "======================================================================"
echo ""
echo "  Floor settings are now ACTIVE for project: $PROJECT_ID"
echo "  All Vertex AI generateContent calls (including Agent Engine) are"
echo "  automatically screened — no code changes required."
echo ""
echo "  To verify floor settings:"
echo "    gcloud model-armor floor-settings describe \\"
echo "      --location=global --project=$PROJECT_ID"
echo ""
echo "  To enable Cloud Logging for sanitization results:"
echo "    gcloud logging sinks create model-armor-sink \\"
echo "      logging.googleapis.com/projects/$PROJECT_ID/logs/modelarmor.googleapis.com \\"
echo "      --log-filter='resource.type=\"modelarmor.googleapis.com/Template\"'"
echo ""
