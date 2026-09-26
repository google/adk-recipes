#!/usr/bin/env python3
"""
Create Firestore Vector Index for RAG Search

This script creates a vector search index on the products collection
to enable semantic search with embeddings.

Usage:
    python ops/create_vector_index.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from google.cloud.firestore_admin_v1 import FirestoreAdminClient  # noqa: E402
from google.cloud.firestore_admin_v1.types import Index  # noqa: E402

# Load environment
load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "project-ddc15d84-7238-4571-a39")
DATABASE_ID = os.getenv("FIRESTORE_DATABASE", "customer-support-db")

print("=" * 80)
print("Creating Firestore Vector Index for RAG Search")
print("=" * 80)
print(f"\nProject: {PROJECT_ID}")
print(f"Database: {DATABASE_ID}")
print("Collection: products")
print("Field: embedding")
print("Dimensions: 768")
print()


def create_vector_index():
    """Create vector index using Firestore Admin API."""

    try:
        # Initialize Firestore Admin client
        FirestoreAdminClient()

        # Build parent path
        parent = f"projects/{PROJECT_ID}/databases/{DATABASE_ID}/collectionGroups/products"

        print("📝 Creating vector index...")
        print(f"   Parent: {parent}")

        # Define the vector index
        # Note: As of 2024, vector indexes are created through the REST API
        # or Firebase console, not through the Python SDK yet

        print("\n⚠️  Vector Index Creation")
        print("=" * 80)
        print("Vector indexes for Firestore cannot be created via Python SDK yet.")
        print("Please create the index using one of these methods:")
        print()
        print("METHOD 1: Firebase Console (Recommended)")
        print("-" * 80)
        print("1. Go to: https://console.firebase.google.com/")
        print(f"2. Select project: {PROJECT_ID}")
        print("3. Navigate to: Firestore Database → Data")
        print("4. Select collection: products")
        print("5. Click on any document with 'embedding' field")
        print("6. You'll see a prompt to create a vector index")
        print("7. Or go to: Build → Firestore Database → Indexes → Composite")
        print()
        print("   Unfortunately, vector indexes are not yet available in the UI.")
        print("   You need to use the REST API or wait for UI support.")
        print()
        print("METHOD 2: REST API")
        print("-" * 80)
        print("Use the following curl command:")
        print()
        print(f"""curl -X POST \\
  'https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/{DATABASE_ID}/collectionGroups/products/indexes' \\
  -H 'Authorization: Bearer $(gcloud auth print-access-token)' \\
  -H 'Content-Type: application/json' \\
  -d '{{
    "fields": [
      {{
        "fieldPath": "embedding",
        "vectorConfig": {{
          "dimension": 768,
          "flat": {{}}
        }}
      }}
    ],
    "queryScope": "COLLECTION"
  }}'""")
        print()
        print("METHOD 3: Disable RAG (Quick Workaround)")
        print("-" * 80)
        print("Edit: customer_support_mas/services/rag_search.py")
        print("Set: USE_RAG = False")
        print("This will use keyword search instead of semantic search.")
        print()
        print("=" * 80)

        return False

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        return False


def verify_index_exists():
    """Check if vector index already exists."""
    try:
        admin_client = FirestoreAdminClient()
        parent = f"projects/{PROJECT_ID}/databases/{DATABASE_ID}/collectionGroups/products"

        print("🔍 Checking for existing indexes...")
        indexes = admin_client.list_indexes(parent=parent)

        vector_index_found = False
        for index in indexes:
            print(f"\n   Index: {index.name}")
            for field in index.fields:
                print(f"   - Field: {field.field_path}")
                if hasattr(field, "vector_config") and field.vector_config:
                    print(f"     Type: VECTOR (dimension: {field.vector_config.dimension})")
                    vector_index_found = True
                elif field.order:
                    print(f"     Type: {Index.IndexField.Order.Name(field.order)}")
                elif field.array_config:
                    print(f"     Type: {Index.IndexField.ArrayConfig.Name(field.array_config)}")

        if vector_index_found:
            print("\n✅ Vector index already exists!")
            return True
        else:
            print("\n⚠️  No vector index found.")
            return False

    except Exception as e:
        print(f"\n⚠️  Could not check indexes: {e}")
        return False


def main():
    """Main execution."""

    # Check if index exists
    if verify_index_exists():
        print("\n✅ Vector index is ready for RAG search!")
        return 0

    # Try to create (will show instructions)
    if create_vector_index():
        print("\n✅ Vector index created successfully!")
        return 0
    else:
        print("\n⚠️  Please create vector index manually using one of the methods above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
