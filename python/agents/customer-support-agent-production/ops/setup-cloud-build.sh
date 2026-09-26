#!/usr/bin/env bash
# ==============================================================================
# One-time Cloud Build setup script (2nd gen triggers)
# Grants IAM roles, creates Artifact Registry repo, Secret Manager secret,
# and creates a Cloud Build GitHub connection + 2nd gen triggers.
#
# Usage:
#   make setup-cloud-build
#   (reads PROJECT_ID, REGION, STAGING_BUCKET from .env automatically)
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Owner or Editor role on the GCP project
#   - GitHub account with access to the repo
# ==============================================================================

set -euo pipefail

if [ $# -lt 3 ]; then
  echo "Usage: $0 <PROJECT_ID> <REGION> <STAGING_BUCKET_NAME>"
  exit 1
fi

PROJECT_ID="$1"
REGION="$2"
STAGING_BUCKET="$3"
GITHUB_OWNER="${4:-}"
AR_REPO="customer-support"
CONNECTION_NAME="github-connection"

# Auto-detect GitHub owner and repo name from git remote URL
# Supports both HTTPS (https://github.com/OWNER/REPO.git) and SSH (git@github.com:OWNER/REPO.git)
# Can be overridden via .env (GITHUB_OWNER, GITHUB_REPO) or script args ($4, $5)
GITHUB_OWNER="${4:-}"
REPO_NAME="${5:-}"

if [ -f .env ]; then
  if [ -z "${GITHUB_OWNER}" ]; then GITHUB_OWNER=$(grep '^GITHUB_OWNER=' .env | cut -d= -f2- || echo ""); fi
  if [ -z "${REPO_NAME}" ];   then REPO_NAME=$(grep '^GITHUB_REPO='  .env | cut -d= -f2- || echo ""); fi
fi

if [ -z "${GITHUB_OWNER}" ] || [ -z "${REPO_NAME}" ]; then
  REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
  if [ -n "${REMOTE_URL}" ]; then
    # Strip .git suffix, then extract owner/repo from both HTTPS and SSH formats
    REMOTE_URL="${REMOTE_URL%.git}"
    GITHUB_OWNER="${GITHUB_OWNER:-$(echo "${REMOTE_URL}" | sed -E 's|.*github\.com[:/]([^/]+)/.*|\1|')}"
    REPO_NAME="${REPO_NAME:-$(echo "${REMOTE_URL}" | sed -E 's|.*github\.com[:/][^/]+/(.+)|\1|')}"
  fi
fi

if [ -z "${GITHUB_OWNER}" ] || [ -z "${REPO_NAME}" ]; then
  echo "ERROR: Could not detect GitHub owner/repo. Add to .env:"
  echo "  GITHUB_OWNER=your-github-username"
  echo "  GITHUB_REPO=your-repo-name"
  exit 1
fi

echo "GitHub owner: ${GITHUB_OWNER}"
echo "GitHub repo:  ${REPO_NAME}"

echo "=== Cloud Build 2nd Gen Setup for project: ${PROJECT_ID} ==="

# Get project number
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
CB_P4SA="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo ""
echo "Cloud Build SA:   ${CB_SA}"
echo "Cloud Build P4SA: ${CB_P4SA}"
echo "Compute SA:       ${COMPUTE_SA}"
echo "Region:         ${REGION}"
echo ""

# ==============================================================================
# 1. Enable required APIs (must be first)
# ==============================================================================
echo "--- Enabling required APIs ---"

declare -a APIS=(
  "cloudbuild.googleapis.com"
  "artifactregistry.googleapis.com"
  "run.googleapis.com"
  "secretmanager.googleapis.com"
  "aiplatform.googleapis.com"
  "firestore.googleapis.com"
  "cloudscheduler.googleapis.com"
)

for API in "${APIS[@]}"; do
  echo "  Enabling ${API}..."
  gcloud services enable "${API}" --project="${PROJECT_ID}" --quiet
done
echo "  ✓ APIs enabled."
echo ""

# ==============================================================================
# 2. Grant IAM roles to Cloud Build service account
# ==============================================================================
echo "--- Granting IAM roles to Cloud Build SA ---"

declare -a ROLES=(
  "roles/datastore.user"                # Firestore access (tests + deploy)
  "roles/aiplatform.user"               # Vertex AI Gemini API (agent evals)
  "roles/aiplatform.admin"              # Agent Engine deployment
  "roles/artifactregistry.writer"       # Push Docker images
  "roles/run.admin"                     # Cloud Run deployment
  "roles/storage.objectAdmin"           # Staging bucket for Agent Engine
  "roles/secretmanager.secretAccessor"  # Read secrets
)

for ROLE in "${ROLES[@]}"; do
  echo "  Granting ${ROLE}..."
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${CB_SA}" \
    --role="${ROLE}" \
    --quiet 2>/dev/null
done

# Cloud Build P4SA needs Secret Manager access to store GitHub OAuth token
echo "  Granting secretmanager.admin to Cloud Build P4SA..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CB_P4SA}" \
  --role="roles/secretmanager.admin" \
  --quiet 2>/dev/null \
  && echo "  ✓ secretmanager.admin granted to P4SA." \
  || echo "  ⚠ Could not grant secretmanager.admin to P4SA"

# Cloud Build needs to act as the Cloud Run service account
echo "  Granting serviceAccountUser on compute SA..."
gcloud iam service-accounts add-iam-policy-binding "${COMPUTE_SA}" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet 2>/dev/null \
  && echo "  ✓ serviceAccountUser granted." \
  || echo "  ⚠ Could not grant serviceAccountUser (may already exist or SA not yet provisioned)"

echo "  ✓ IAM roles granted."
echo ""

# ==============================================================================
# 3. Create Artifact Registry repository
# ==============================================================================
echo "--- Creating Artifact Registry repo: ${AR_REPO} ---"

if gcloud artifacts repositories describe "${AR_REPO}" \
    --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "  Repository already exists, skipping."
else
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="Customer support app Docker images"
  echo "  ✓ Repository created."
fi
echo ""

# ==============================================================================
# 4. Create Secret Manager secret
# ==============================================================================
echo "--- Setting up Secret Manager ---"

if gcloud secrets describe staging-bucket --project="${PROJECT_ID}" &>/dev/null; then
  echo "  Secret 'staging-bucket' already exists, updating..."
  echo -n "${STAGING_BUCKET}" | gcloud secrets versions add staging-bucket \
    --data-file=- --project="${PROJECT_ID}"
else
  echo "  Creating secret 'staging-bucket'..."
  echo -n "${STAGING_BUCKET}" | gcloud secrets create staging-bucket \
    --data-file=- --project="${PROJECT_ID}" \
    --replication-policy="automatic"
fi
echo "  ✓ Secret ready."
echo ""

# ==============================================================================
# 5. Create Cloud Build 2nd gen GitHub connection
# ==============================================================================
echo "--- Setting up Cloud Build 2nd gen GitHub connection ---"

if gcloud builds connections describe "${CONNECTION_NAME}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --format='value(installationState.stage)' 2>/dev/null | grep -q "COMPLETE"; then
  echo "  Connection '${CONNECTION_NAME}' already authorized, skipping."
else
  # Create connection if it doesn't exist yet
  if ! gcloud builds connections describe "${CONNECTION_NAME}" \
      --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "  Creating GitHub connection..."
    gcloud builds connections create github "${CONNECTION_NAME}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}"
  fi

  # Get OAuth URL and prompt user to authorize
  OAUTH_URL=$(gcloud builds connections describe "${CONNECTION_NAME}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --format='value(installationState.actionUri)' 2>/dev/null || echo "")

  echo ""
  echo "  ============================================================"
  echo "  ACTION REQUIRED: Authorize Cloud Build to access GitHub"
  echo "  ============================================================"
  echo "  1. Open this URL in your browser:"
  echo ""
  echo "     ${OAUTH_URL}"
  echo ""
  echo "  2. Log in with your GitHub account and authorize Cloud Build."
  echo "  3. Come back here and press ENTER to continue."
  echo "  ============================================================"
  read -r -p "  Press ENTER after completing GitHub authorization... "

  # Poll until connection is COMPLETE
  echo "  Waiting for authorization to complete..."
  for i in $(seq 1 24); do
    STATE=$(gcloud builds connections describe "${CONNECTION_NAME}" \
      --region="${REGION}" --project="${PROJECT_ID}" \
      --format='value(installationState.stage)' 2>/dev/null || echo "UNKNOWN")
    if [ "${STATE}" = "COMPLETE" ]; then
      echo "  ✓ Connection authorized."
      break
    fi
    if [ "${i}" = "24" ]; then
      echo "  ERROR: Connection still in state '${STATE}' after 2 minutes."
      echo "  Please complete the GitHub authorization and re-run: make setup-cloud-build"
      exit 1
    fi
    echo "  State: ${STATE} — waiting 5s..."
    sleep 5
  done
fi
echo ""

# ==============================================================================
# 6. Link GitHub repository to connection
# ==============================================================================
echo "--- Linking GitHub repository to connection ---"

REPO_URI="https://github.com/$(gcloud builds connections describe "${CONNECTION_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(githubConfig.authorizerCredential.oauthTokenSecretVersion)' 2>/dev/null | \
  xargs -I{} echo '' || echo '')${GITHUB_OWNER}/${REPO_NAME}"

# Try to get the repo resource name (it may already be linked, possibly under a different name)
REPO_RESOURCE=$(gcloud builds repositories list \
  --connection="${CONNECTION_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(name)' 2>/dev/null | grep -i "${REPO_NAME}" | head -1 || echo "")

# Fallback: pick any linked repo if only one exists
if [ -z "${REPO_RESOURCE}" ]; then
  REPO_RESOURCE=$(gcloud builds repositories list \
    --connection="${CONNECTION_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(name)' 2>/dev/null | head -1 || echo "")
  if [ -n "${REPO_RESOURCE}" ]; then
    echo "  Found linked repository: ${REPO_RESOURCE}"
  fi
fi

# Ensure full resource path (gcloud may return short name only)
if [ -n "${REPO_RESOURCE}" ] && [[ "${REPO_RESOURCE}" != projects/* ]]; then
  REPO_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION_NAME}/repositories/${REPO_RESOURCE}"
fi

if [ -n "${REPO_RESOURCE}" ]; then
  echo "  Repository already linked: ${REPO_RESOURCE}"
else
  # Resolve GitHub owner: arg > .env > prompt
  if [ -z "${GITHUB_OWNER}" ] && [ -f .env ]; then
    GITHUB_OWNER=$(grep '^GITHUB_OWNER=' .env | cut -d= -f2- || echo "")
  fi
  if [ -z "${GITHUB_OWNER}" ]; then
    echo "  Enter your GitHub username (owner of ${REPO_NAME}):"
    read -r GITHUB_OWNER
  fi
  echo "  Linking https://github.com/${GITHUB_OWNER}/${REPO_NAME}..."
  gcloud builds repositories create "${REPO_NAME}" \
    --remote-uri="https://github.com/${GITHUB_OWNER}/${REPO_NAME}.git" \
    --connection="${CONNECTION_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}"
  REPO_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION_NAME}/repositories/$(gcloud builds repositories list --connection="${CONNECTION_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(name)' | grep -i "${REPO_NAME}" | head -1 | sed 's|.*/||')"
  echo "  ✓ Repository linked: ${REPO_RESOURCE}"
fi
echo ""

# ==============================================================================
# 7. Create 2nd gen triggers via REST API
# (gcloud CLI uses v1 API which does not support 2nd gen --repository flag)
# ==============================================================================
echo "--- Creating Cloud Build 2nd gen triggers ---"

TOKEN=$(gcloud auth print-access-token)
API="https://cloudbuild.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/triggers"

# 2nd gen triggers must be created from the Cloud Console.
# Print exact instructions for each trigger.
echo ""
echo "  ============================================================"
echo "  ACTION REQUIRED: Create triggers in Cloud Console"
echo "  ============================================================"
echo "  Go to: https://console.cloud.google.com/cloud-build/triggers;region=${REGION}?project=${PROJECT_ID}"
echo "  Click 'Create Trigger' for each trigger below:"
echo ""
echo "  [1] ci-cd-push-main  (push to main → standard eval + deploy)"
echo "      Event:        Push to a branch"
echo "      Branch:       ^main$"
echo "      Config file:  cloudbuild/cloudbuild-deploy.yaml"
echo "      Substitutions:"
echo "        _EVAL_PROFILE        = standard"
echo "        _GOOGLE_CLOUD_LOCATION = ${REGION}"
echo "      Repository:   ${REPO_RESOURCE}"
echo ""
echo "  [2] ci-push-develop  (push to develop → standard eval)"
echo "      Event:        Push to a branch"
echo "      Branch:       ^develop$"
echo "      Config file:  cloudbuild/cloudbuild.yaml"
echo "      Substitutions:"
echo "        _EVAL_PROFILE          = standard"
echo "        _GOOGLE_CLOUD_LOCATION = ${REGION}"
echo "      Repository:   ${REPO_RESOURCE}"
echo ""
echo "  [3] ci-nightly-full  (manual/nightly → full eval)"
echo "      Event:        Manual invocation"
echo "      Branch:       main"
echo "      Config file:  cloudbuild/cloudbuild-nightly.yaml"
echo "      Substitutions:"
echo "        _EVAL_PROFILE          = full"
echo "        _GOOGLE_CLOUD_LOCATION = ${REGION}"
echo "      Repository:   ${REPO_RESOURCE}"
echo "  ============================================================"

echo ""

# ==============================================================================
# 8. Set up nightly Cloud Scheduler job
# ==============================================================================
echo "--- Setting up nightly Cloud Scheduler job ---"

TRIGGER_ID=$(gcloud builds triggers list \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --filter="name=ci-nightly-full" \
  --format="value(id)")

if gcloud scheduler jobs describe nightly-full-eval \
    --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "  Scheduler job 'nightly-full-eval' already exists, skipping."
else
  gcloud scheduler jobs create http nightly-full-eval \
    --schedule="0 0 * * *" \
    --location="${REGION}" \
    --uri="https://cloudbuild.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/triggers/${TRIGGER_ID}:run" \
    --http-method=POST \
    --oauth-service-account-email="${COMPUTE_SA}" \
    --message-body='{"branchName": "main"}' \
    --time-zone="UTC" \
    --project="${PROJECT_ID}"
  echo "  ✓ Nightly scheduler job created."
fi
echo ""

# ==============================================================================
# Done
# ==============================================================================
echo "======================================================================"
echo "  Cloud Build 2nd Gen Setup Complete"
echo "======================================================================"
echo ""
echo "  Triggers created:"
echo "    ci-pull-request  → PR to main    → fast eval"
echo "    ci-cd-push-main  → push to main  → standard eval + deploy"
echo "    ci-push-develop  → push to dev   → standard eval"
echo "    ci-nightly-full  → nightly cron  → full eval"
echo ""
echo "  To verify triggers:"
echo "    gcloud builds triggers list --region=${REGION} --project=${PROJECT_ID}"
echo ""
echo "  To manually run the nightly full eval:"
echo "    gcloud builds triggers run ci-nightly-full --region=${REGION} --branch=main --project=${PROJECT_ID}"
echo ""
