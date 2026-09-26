"""
Add Embeddings to Product Catalog for RAG Search
=================================================
Run this ONCE after seeding your database to add vector embeddings.

Usage:
    python ops/add_embeddings.py --project PROJECT_ID --database DATABASE_ID
"""

import argparse

from google import genai
from google.cloud import firestore


def add_embeddings_to_products(project_id: str, database_id: str, location: str = "us-central1"):
    """Add vector embeddings to all products for semantic search."""

    print("=" * 60)
    print("ADDING EMBEDDINGS TO PRODUCTS")
    print("=" * 60)
    print(f"Project: {project_id}")
    print(f"Database: {database_id}")
    print(f"Location: {location}")

    # Initialize genai client and Firestore
    client = genai.Client(vertexai=True, project=project_id, location=location)
    db = firestore.Client(project=project_id, database=database_id)

    print("\n⏳ Using embedding model (text-embedding-004)...")

    # Get all products
    products = list(db.collection("products").stream())
    print(f"\n📦 Found {len(products)} products to process")

    # Process each product
    for i, doc in enumerate(products, 1):
        data = doc.to_dict()

        # Create rich searchable text combining all relevant fields
        search_text_parts = [
            data.get("name", ""),
            data.get("description", ""),
            data.get("category", ""),
            " ".join(data.get("keywords", [])),
        ]

        # Add specs if available
        if "specs" in data:
            specs = data["specs"]
            search_text_parts.append(" ".join(str(v) for v in specs.values()))

        search_text = " ".join(search_text_parts)

        print(f"\n[{i}/{len(products)}] Processing: {doc.id} - {data.get('name')}")
        print(f"   Search text: {search_text[:100]}...")

        # Generate embedding
        try:
            embeddings_response = client.models.embed_content(model="text-embedding-004", contents=[search_text])
            embedding_vector = embeddings_response.embeddings[0].values

            # Update Firestore document with embedding
            doc.reference.update(
                {
                    "embedding": embedding_vector,
                    "search_text": search_text,  # Store for debugging
                }
            )

            print(f"   ✓ Added embedding (dimension: {len(embedding_vector)})")

        except Exception as e:
            print(f"   ✗ Error: {e}")
            continue

    print("\n" + "=" * 60)
    print("✅ EMBEDDINGS ADDED SUCCESSFULLY!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Your products now have vector embeddings")
    print("2. Use semantic search in your agent")
    print("3. Test with queries like 'gaming computer' or 'comfortable seating'")
    print("\nNote: Firestore vector search requires an index. Create it with:")
    print("gcloud firestore indexes composite create \\")
    print("  --collection-group=products \\")
    print("  --query-scope=COLLECTION \\")
    print('  --field-config field-path=embedding,vector-config=\'{"dimension":"768","flat": {}}\' \\')
    print(f"  --database={database_id}")


def main():
    parser = argparse.ArgumentParser(description="Add RAG embeddings to products")
    parser.add_argument("--project", type=str, required=True, help="GCP Project ID")
    parser.add_argument("--database", type=str, default="customer-support-db", help="Firestore database ID")
    parser.add_argument("--location", type=str, default="us-central1", help="GCP location")

    args = parser.parse_args()

    add_embeddings_to_products(project_id=args.project, database_id=args.database, location=args.location)


if __name__ == "__main__":
    main()
