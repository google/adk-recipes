# Utility Scripts

This directory contains utility scripts for setup and maintenance tasks.

## Scripts

### add_embeddings.py

Adds vector embeddings to the product catalog for RAG (semantic) search.

**Purpose:** Run this ONCE after seeding your database to enable semantic search on products.

**Usage:**
```bash
python ops/add_embeddings.py --project PROJECT_ID --database DATABASE_ID
```

**Example:**
```bash
python ops/add_embeddings.py \
  --project project-ddc15d84-7238-4571-a39 \
  --database customer-support-db
```

**What it does:**
1. Connects to your Firestore database
2. Loads the Vertex AI text-embedding-004 model
3. For each product, creates a rich embedding from name, description, category, and keywords
4. Stores the 768-dimensional embedding vector in the product document
5. Enables semantic search via `customer_support_mas/services/rag_search.py`

**When to run:**
- After initial database seeding
- After adding new products in bulk
- After updating product descriptions

**Note:** This script modifies your Firestore database. Make sure you're targeting the correct project and database!

## Related

- Database seeding: `python -m customer_support_mas.database.fixtures`
- RAG search implementation: `customer_support_mas/services/rag_search.py`
- Product tools: `customer_support_mas/tools/product_tools.py`
