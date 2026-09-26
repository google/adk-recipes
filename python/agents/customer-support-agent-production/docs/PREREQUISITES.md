# Prerequisites and Setup Guide

Complete guide for setting up GCP resources, APIs, IAM permissions, and dependencies before deploying the multi-agent customer support system.


## 1. Required GCP APIs

The following APIs must be enabled before deployment:

### Core AI/ML APIs
- ✅ **Vertex AI API** (`aiplatform.googleapis.com`)
  - Agent Engine deployment
  - Gemini models (2.5 Pro, 2.5 Flash)
  - Vector embeddings (text-embedding-004)

### Database & Storage APIs
- ✅ **Firestore API** (`firestore.googleapis.com`)
  - NoSQL database
  - Vector search for RAG

- ✅ **Cloud Storage API** (`storage.googleapis.com`)
  - Staging bucket for deployments
  - Container image storage

### Infrastructure APIs
- ✅ **Cloud Run API** (`run.googleapis.com`)
  - Frontend hosting
  - Backend API hosting

- ✅ **Cloud Build API** (`cloudbuild.googleapis.com`)
  - Container image builds
  - CI/CD pipelines

- ✅ **Artifact Registry API** (`artifactregistry.googleapis.com`)
  - Container image registry

### Management APIs
- ✅ **Cloud Resource Manager API** (`cloudresourcemanager.googleapis.com`)
  - Project management

- ✅ **IAM API** (`iam.googleapis.com`)
  - Service account management

- ✅ **Cloud Logging API** (`logging.googleapis.com`)
  - Application logs

- ✅ **Cloud Monitoring API** (`monitoring.googleapis.com`)
  - Performance monitoring

## 2. IAM Roles and Permissions

### Service Account Roles

Create a service account for the application with these roles:

| Role | Purpose | Required For |
|------|---------|--------------|
| `roles/aiplatform.user` | Access Vertex AI services | Agent Engine, Gemini API |
| `roles/aiplatform.serviceAgent` | Vertex AI operations | Model inference, embeddings |
| `roles/datastore.user` | Read/write Firestore | Database operations |
| `roles/storage.objectAdmin` | GCS bucket access | Deployment staging |
| `roles/logging.logWriter` | Write application logs | Observability |
| `roles/run.invoker` | Invoke Cloud Run services | Backend API calls |

### User/Developer Roles

Your GCP user account needs these roles for deployment:

| Role | Purpose |
|------|---------|
| `roles/aiplatform.admin` | Deploy to Agent Engine |
| `roles/datastore.owner` | Create/manage Firestore database |
| `roles/storage.admin` | Create/manage GCS buckets |
| `roles/run.admin` | Deploy to Cloud Run |
| `roles/iam.serviceAccountUser` | Use service accounts |
| `roles/resourcemanager.projectIamAdmin` | Grant IAM permissions |

## 3. GCP Resources Setup

### Required Resources

1. **GCP Project**
   - Billing enabled
   - Project ID (e.g., `my-customer-support-123`)

2. **GCS Bucket**
   - For Agent Engine staging
   - Regional bucket (same region as services)
   - Example: `gs://my-project-staging`

3. **Firestore Database**
   - Firestore Native mode
   - Multi-region or regional
   - Database ID: `customer-support-db` (or custom)

4. **Service Account**
   - Name: `customer-support-agent`
   - Email: `customer-support-agent@PROJECT_ID.iam.gserviceaccount.com`

## 4. Quick Setup (Automated)

For advanced or manual setup, see the [Manual Setup section below](#5-manual-setup).

### Option A: All-in-One Setup

```bash
# 1. Authenticate
gcloud auth login
gcloud auth application-default login

# 2. Set project
gcloud config set project YOUR_PROJECT_ID

# 3. Run automated setup
./ops/setup_gcp.sh        # Enable APIs, create service account, grant permissions
./ops/setup_firestore.sh  # Create database, seed data
```

### Option B: Step-by-Step Setup

```bash
# Step 1: Setup GCP prerequisites
./ops/setup_gcp.sh

# Step 2: Setup Firestore database
./ops/setup_firestore.sh

# Step 3: Configure environment
cp .env.example .env
nano .env  # Edit with your values

# Step 4: Deploy to Agent Engine
python deployment/deploy.py
```

## 5. Manual Setup

If you prefer manual setup or the automated scripts fail:

### Step 1: Enable APIs

```bash
PROJECT_ID="your-project-id"

# Enable all required APIs
gcloud services enable aiplatform.googleapis.com \
  firestore.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  --project="$PROJECT_ID"
```

### Step 2: Create Service Account

```bash
SERVICE_ACCOUNT_NAME="customer-support-agent"

# Create service account
gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
  --display-name="Customer Support Agent" \
  --project="$PROJECT_ID"

# Get service account email
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```

### Step 3: Grant IAM Roles to Service Account

```bash
# Grant roles
for role in \
  roles/aiplatform.user \
  roles/aiplatform.serviceAgent \
  roles/datastore.user \
  roles/storage.objectAdmin \
  roles/logging.logWriter \
  roles/run.invoker
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SERVICE_ACCOUNT_EMAIL" \
    --role="$role"
done
```

### Step 4: Grant IAM Roles to Your User

```bash
# Get your email
USER_EMAIL=$(gcloud config get-value account)

# Grant roles
for role in \
  roles/aiplatform.admin \
  roles/datastore.owner \
  roles/storage.admin \
  roles/run.admin \
  roles/iam.serviceAccountUser
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:$USER_EMAIL" \
    --role="$role"
done
```

### Step 5: Create GCS Bucket

```bash
BUCKET_NAME="${PROJECT_ID}-staging"
LOCATION="us-central1"

# Create bucket
gsutil mb -p "$PROJECT_ID" -l "$LOCATION" "gs://$BUCKET_NAME"

# Grant service account access
gsutil iam ch "serviceAccount:$SERVICE_ACCOUNT_EMAIL:roles/storage.objectAdmin" \
  "gs://$BUCKET_NAME"
```

### Step 6: Create Firestore Database

```bash
DATABASE_ID="customer-support-db"

# Create Firestore database
gcloud firestore databases create \
  --database="$DATABASE_ID" \
  --location="nam5" \
  --type=firestore-native \
  --project="$PROJECT_ID"
```

**Firestore Location Mapping:**
- US regions → `nam5` (US multi-region)
- Europe regions → `eur3` (Europe multi-region)
- Asia regions → `asia-southeast1`

### Step 7: Seed Database

```bash
# Seed with sample data
PYTHONPATH=. python -m customer_support_mas.database.fixtures

# Optional: Add vector embeddings for RAG
PYTHONPATH=. python ops/add_embeddings.py
```

## 6. Verify Setup

### Check APIs

```bash
# List enabled APIs
gcloud services list --enabled --project="$PROJECT_ID" | grep -E "aiplatform|firestore|run"
```

Expected output:
```
aiplatform.googleapis.com       Vertex AI API
firestore.googleapis.com        Cloud Firestore API
run.googleapis.com              Cloud Run Admin API
```

### Check Service Account

```bash
# Describe service account
gcloud iam service-accounts describe customer-support-agent@$PROJECT_ID.iam.gserviceaccount.com

# List roles
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:customer-support-agent@$PROJECT_ID.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```

### Check GCS Bucket

```bash
# List buckets
gsutil ls -p "$PROJECT_ID"

# Check bucket permissions
gsutil iam get "gs://$BUCKET_NAME"
```

### Check Firestore Database

```bash
# Describe database
gcloud firestore databases describe customer-support-db --project="$PROJECT_ID"

# List collections (after seeding)
gcloud firestore collections list --database=customer-support-db --project="$PROJECT_ID"
```

Expected collections:
- products
- orders
- invoices
- users
- sessions

### Test Deployment

```bash
# Test local agent
python deployment/deploy.py --action test_local

# Deploy to Agent Engine
python deployment/deploy.py --action deploy
```

## See Also

- [ENV_SETUP.md](./ENV_SETUP.md) - Environment configuration
- [DEPLOYMENT.md](./DEPLOYMENT.md) - Deployment guide
- [ARCHITECTURE.md](./ARCHITECTURE.md) - System architecture
- [README.md](../README.md) - Main documentation
