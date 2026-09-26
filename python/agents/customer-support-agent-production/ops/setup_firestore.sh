#!/bin/bash
# ============================================================================
# Firestore Database Setup Script
# ============================================================================
# Creates Firestore database and seeds initial data
#
# Usage:
#   ./ops/setup_firestore.sh
# ============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Load from .env
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PROJECT_ID=${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}
LOCATION=${GOOGLE_CLOUD_LOCATION:-us-central1}
DATABASE_ID=${FIRESTORE_DATABASE:-customer-support-db}

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Firestore Database Setup                                  ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ============================================================================
# 1. Check if database exists
# ============================================================================

echo -e "${YELLOW}[1/3] Checking Firestore database...${NC}"
echo ""

# Try to describe the database
if gcloud firestore databases describe "$DATABASE_ID" --project="$PROJECT_ID" &>/dev/null; then
    echo -e "  ${YELLOW}⚠${NC} Database already exists: $DATABASE_ID"
    echo -e "  Skipping database creation"
else
    echo -e "  Creating Firestore database: ${BLUE}$DATABASE_ID${NC}"
    echo -e "  Location: ${BLUE}$LOCATION${NC}"
    echo ""

    # Determine Firestore location code
    # Map Cloud Run regions to Firestore multi-region codes
    case $LOCATION in
        us-central1|us-east1|us-west1|us-east4)
            FIRESTORE_LOCATION="nam5"  # US multi-region
            ;;
        europe-west1|europe-west2|europe-west3)
            FIRESTORE_LOCATION="eur3"  # Europe multi-region
            ;;
        asia-northeast1|asia-southeast1)
            FIRESTORE_LOCATION="asia-southeast1"  # Asia
            ;;
        *)
            FIRESTORE_LOCATION="nam5"  # Default to US
            ;;
    esac

    echo -e "  Using Firestore location: ${BLUE}$FIRESTORE_LOCATION${NC}"
    echo ""

    gcloud firestore databases create \
        --database="$DATABASE_ID" \
        --location="$FIRESTORE_LOCATION" \
        --type=firestore-native \
        --project="$PROJECT_ID"

    echo -e "  ${GREEN}✓${NC} Database created"
fi

echo ""

# ============================================================================
# 2. Seed database with sample data
# ============================================================================

echo -e "${YELLOW}[2/3] Seeding database with sample data...${NC}"
echo ""

echo -e "  Running seed script..."
if PYTHONPATH=. python -m customer_support_mas.database.fixtures --project "$PROJECT_ID" --database "$DATABASE_ID"; then
    echo -e "  ${GREEN}✓${NC} Database seeded successfully"
else
    echo -e "  ${RED}✗${NC} Error seeding database"
    exit 1
fi

echo ""

# ============================================================================
# 3. Optional: Add vector embeddings for RAG
# ============================================================================

echo -e "${YELLOW}[3/3] Vector Embeddings (Optional - for RAG)${NC}"
echo ""

read -p "Do you want to add vector embeddings for RAG search? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "  Adding vector embeddings..."
    if PYTHONPATH=. python ops/add_embeddings.py --project "$PROJECT_ID" --database "$DATABASE_ID"; then
        echo -e "  ${GREEN}✓${NC} Vector embeddings added"
    else
        echo -e "  ${YELLOW}⚠${NC} Error adding embeddings (optional feature)"
    fi
else
    echo -e "  ${BLUE}Skipped.${NC} You can add embeddings later with:"
    echo -e "    ${YELLOW}python ops/add_embeddings.py${NC}"
fi

echo ""

# ============================================================================
# Summary
# ============================================================================

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Firestore Setup Complete!                                 ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Database Configuration:${NC}"
echo -e "  Project:  ${GREEN}$PROJECT_ID${NC}"
echo -e "  Database: ${GREEN}$DATABASE_ID${NC}"
echo ""
echo -e "${BLUE}Collections Created:${NC}"
echo -e "  - ${GREEN}products${NC}     (10 sample products)"
echo -e "  - ${GREEN}orders${NC}       (3 sample orders)"
echo -e "  - ${GREEN}invoices${NC}     (3 sample invoices)"
echo -e "  - ${GREEN}users${NC}        (1 sample user)"
echo -e "  - ${GREEN}sessions${NC}     (ready for conversations)"
echo ""
echo -e "${GREEN}✓ Database ready for use!${NC}"
echo ""
