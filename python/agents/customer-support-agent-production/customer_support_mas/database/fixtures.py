"""
Database Seeder - Populate Firestore with Demo Data
=====================================================
Usage:
    python seed_database.py --project PROJECT_ID --database DATABASE_ID
    python seed_database.py --project PROJECT_ID --database DATABASE_ID --clear

Data Model:
-----------
1. GLOBAL DATA (accessible by everyone):
   - products: Product catalog
   - inventory: Stock levels
   - reviews: Product reviews

2. USER-SPECIFIC DATA (linked to user accounts):
   - orders: Purchase history (customer_id = user_id)
   - invoices: Billing records
   - payments: Payment records
   - refund_eligibility: Refund rules per order

3. USER TYPES:
   - Demo Users: Pre-seeded with order history (demo@example.com, jane@example.com)
   - New Users: Start fresh, can browse products but have no order history
"""

import argparse
import os
from datetime import datetime, timedelta

import bcrypt

# Load .env file if available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from google.cloud import firestore


# =============================================================================
# DYNAMIC DATE HELPERS (for realistic test data)
# =============================================================================
def _days_ago(days: int) -> str:
    """Return date string for N days ago."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# =============================================================================
# DEMO USER CREDENTIALS
# =============================================================================
# These are known user IDs that link to pre-seeded order/billing data.
# When a user logs in with these emails, they get access to the demo data.

DEMO_USERS = {
    "demo-user-001": {
        "user_id": "demo-user-001",
        "email": "demo@example.com",
        "name": "Demo User",
        "password_hash": bcrypt.hashpw("demo123".encode(), bcrypt.gensalt()).decode(),
        "tier": "Gold",
        "created_at": datetime(2024, 1, 1),
        "last_login": None,
        "is_demo": True,
    },
    "demo-user-002": {
        "user_id": "demo-user-002",
        "email": "jane@example.com",
        "name": "Jane Smith",
        "password_hash": bcrypt.hashpw("jane123".encode(), bcrypt.gensalt()).decode(),
        "tier": "Silver",
        "created_at": datetime(2024, 6, 1),
        "last_login": None,
        "is_demo": True,
    },
}

# Email to user_id mapping for easy lookup
DEMO_USER_EMAILS = {
    "demo@example.com": "demo-user-001",
    "jane@example.com": "demo-user-002",
}


def get_sample_data():
    """Returns all sample data for the demo.

    Data Structure:
    ---------------
    - Products/Inventory/Reviews: Global data (no user linking)
    - Orders/Invoices/Payments/Refunds: Linked to demo user IDs

    Demo Users:
    -----------
    - demo@example.com (demo123): Gold tier, 3 orders, 2 invoices
    - jane@example.com (jane123): Silver tier, 1 order
    """

    # =========================================================================
    # GLOBAL DATA - Accessible by all users
    # =========================================================================

    products = {
        "PROD-001": {
            "name": "ProBook Laptop 15",
            "price": 999.99,
            "category": "Electronics",
            "description": "High-performance laptop with Intel i7",
            "keywords": ["laptop", "computer", "notebook", "probook"],
            "specs": {"processor": "Intel Core i7-12700H", "ram": "16GB DDR5", "storage": "512GB NVMe SSD"},
            "warranty": "2 years",
            "rating": 4.5,
        },
        "PROD-006": {
            "name": "ROG Gaming Laptop",
            "price": 1499.99,
            "category": "Electronics",
            "description": "High-performance gaming laptop with RTX 4060 graphics card",
            "keywords": ["laptop", "gaming", "computer", "notebook", "rog", "gaming laptop"],
            "specs": {
                "processor": "Intel Core i7-13700H",
                "gpu": "NVIDIA RTX 4060",
                "ram": "32GB DDR5",
                "storage": "1TB NVMe SSD",
                "display": "15.6 inch 144Hz",
            },
            "warranty": "2 years",
            "rating": 4.8,
        },
        "PROD-002": {
            "name": "Wireless Headphones Pro",
            "price": 199.99,
            "category": "Electronics",
            "description": "Premium noise-canceling wireless headphones",
            "keywords": ["headphones", "audio", "wireless", "bluetooth"],
            "specs": {"driver": "40mm", "battery": "30 hours", "noise_canceling": True},
            "warranty": "1 year",
            "rating": 4.7,
        },
        "PROD-003": {
            "name": "Mechanical Gaming Keyboard",
            "price": 149.99,
            "category": "Electronics",
            "description": "RGB mechanical keyboard with Cherry MX switches",
            "keywords": ["keyboard", "gaming", "mechanical", "rgb"],
            "specs": {"switches": "Cherry MX Red", "layout": "Full-size", "backlighting": "RGB"},
            "warranty": "2 years",
            "rating": 4.6,
        },
        "PROD-004": {
            "name": "Ergonomic Office Chair",
            "price": 449.99,
            "category": "Furniture",
            "description": "Premium ergonomic chair with lumbar support",
            "keywords": ["chair", "office", "ergonomic", "furniture"],
            "specs": {"material": "Mesh back", "adjustable": "Height, armrests, lumbar"},
            "warranty": "5 years",
            "rating": 4.4,
        },
        "PROD-005": {
            "name": "Standing Desk Pro",
            "price": 599.99,
            "category": "Furniture",
            "description": "Electric sit-stand desk with memory presets",
            "keywords": ["desk", "standing", "office", "furniture"],
            "specs": {"dimensions": "60x30 inches", "height_range": "25-51 inches", "motor": "Dual"},
            "warranty": "10 years",
            "rating": 4.8,
        },
    }

    inventory = {
        "PROD-001": {"total_stock": 45, "warehouses": {"US-West": 20, "US-East": 15, "EU": 10}},
        "PROD-002": {"total_stock": 120, "warehouses": {"US-West": 50, "US-East": 40, "EU": 30}},
        "PROD-003": {"total_stock": 78, "warehouses": {"US-West": 30, "US-East": 28, "EU": 20}},
        "PROD-004": {"total_stock": 23, "warehouses": {"US-West": 10, "US-East": 8, "EU": 5}},
        "PROD-005": {"total_stock": 15, "warehouses": {"US-West": 8, "US-East": 5, "EU": 2}},
        "PROD-006": {"total_stock": 32, "warehouses": {"US-West": 15, "US-East": 12, "EU": 5}},
    }

    reviews = {
        "PROD-001": {
            "avg_rating": 4.5,
            "total_reviews": 234,
            "recent_reviews": [
                {"user": "TechFan", "rating": 5, "comment": "Excellent performance!"},
                {"user": "Student123", "rating": 4, "comment": "Great for coding."},
            ],
        },
        "PROD-002": {
            "avg_rating": 4.7,
            "total_reviews": 512,
            "recent_reviews": [
                {"user": "MusicLover", "rating": 5, "comment": "Best noise canceling!"},
                {"user": "Commuter", "rating": 5, "comment": "Perfect for travel."},
            ],
        },
        "PROD-006": {
            "avg_rating": 4.8,
            "total_reviews": 189,
            "recent_reviews": [
                {"user": "GamerPro", "rating": 5, "comment": "Runs all games smoothly at high settings!"},
                {"user": "Streamer99", "rating": 5, "comment": "Perfect for streaming and gaming!"},
            ],
        },
    }

    # =========================================================================
    # USER-SPECIFIC DATA - Linked to demo user IDs
    # =========================================================================
    # IMPORTANT: customer_id must match user_id from the users collection
    # demo-user-001 = demo@example.com (Gold tier, active shopper)
    # demo-user-002 = jane@example.com (Silver tier, occasional shopper)

    orders = {
        # Demo User 001's orders (demo@example.com) - 3 orders
        "ORD-12345": {
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": "2025-01-15",
            "status": "In Transit",
            "carrier": "FastShip",
            "tracking_number": "FS789456123",
            "estimated_delivery": "2025-01-20",
            "items": [
                {"product_id": "PROD-001", "name": "ProBook Laptop 15", "qty": 1, "price": 999.99},
                {"product_id": "PROD-002", "name": "Wireless Headphones Pro", "qty": 1, "price": 199.99},
            ],
            "subtotal": 1199.98,
            "tax": 96.00,
            "total": 1295.98,
            "shipping_address": {
                "street": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "zip": "94102",
                "country": "USA",
            },
            "timeline": [
                {"date": "2025-01-15", "event": "Order placed"},
                {"date": "2025-01-16", "event": "Processing complete"},
                {"date": "2025-01-17", "event": "Shipped"},
                {"date": "2025-01-18", "event": "In transit"},
            ],
        },
        "ORD-67890": {
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": _days_ago(10),  # Ordered 10 days ago
            "status": "Delivered",
            "carrier": "QuickPost",
            "tracking_number": "QP456789012",
            "delivered_date": _days_ago(5),  # Delivered 5 days ago (ELIGIBLE for refund)
            "items": [{"product_id": "PROD-002", "name": "Wireless Headphones Pro", "qty": 1, "price": 199.99}],
            "subtotal": 199.99,
            "tax": 16.00,
            "total": 215.99,
            "shipping_address": {
                "street": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "zip": "94102",
                "country": "USA",
            },
            "timeline": [
                {"date": _days_ago(10), "event": "Order placed"},
                {"date": _days_ago(8), "event": "Shipped"},
                {"date": _days_ago(5), "event": "Delivered"},
            ],
        },
        "ORD-11111": {
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": _days_ago(50),  # Ordered 50 days ago
            "status": "Delivered",
            "carrier": "FastShip",
            "delivered_date": _days_ago(45),  # Delivered 45 days ago (NOT ELIGIBLE - past 30-day window)
            "items": [{"product_id": "PROD-004", "name": "Ergonomic Office Chair", "qty": 1, "price": 449.99}],
            "subtotal": 449.99,
            "tax": 36.00,
            "total": 485.99,
            "shipping_address": {
                "street": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "zip": "94102",
                "country": "USA",
            },
            "timeline": [
                {"date": _days_ago(50), "event": "Order placed"},
                {"date": _days_ago(45), "event": "Delivered"},
            ],
        },
        # Demo User 002's orders (jane@example.com) - 1 order
        "ORD-22222": {
            "customer_id": "demo-user-002",  # ← Links to jane@example.com
            "date": "2025-01-12",
            "status": "Processing",
            "items": [{"product_id": "PROD-005", "name": "Standing Desk Pro", "qty": 1, "price": 599.99}],
            "subtotal": 599.99,
            "tax": 48.00,
            "total": 647.99,
            "shipping_address": {
                "street": "456 Oak Ave",
                "city": "New York",
                "state": "NY",
                "zip": "10001",
                "country": "USA",
            },
            "timeline": [
                {"date": "2025-01-12", "event": "Order placed"},
                {"date": "2025-01-13", "event": "Processing"},
            ],
        },
    }

    invoices = {
        # Demo User 001's invoices
        "INV-2025-001": {
            "order_id": "ORD-12345",
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": "2025-01-15",
            "due_date": "2025-02-15",
            "status": "Pending",
            "items": [
                {"description": "ProBook Laptop 15", "qty": 1, "price": 999.99},
                {"description": "Wireless Headphones Pro", "qty": 1, "price": 199.99},
            ],
            "subtotal": 1199.98,
            "tax": 96.00,
            "total": 1295.98,
        },
        "INV-2025-002": {
            "order_id": "ORD-67890",
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": "2025-01-10",
            "status": "Paid",
            "items": [{"description": "Wireless Headphones Pro", "qty": 1, "price": 199.99}],
            "subtotal": 199.99,
            "tax": 16.00,
            "total": 215.99,
        },
        "INV-2024-003": {
            "order_id": "ORD-11111",
            "customer_id": "demo-user-001",  # ← Links to demo@example.com
            "date": "2024-12-20",
            "status": "Paid",
            "items": [{"description": "Ergonomic Office Chair", "qty": 1, "price": 449.99}],
            "subtotal": 449.99,
            "tax": 36.00,
            "total": 485.99,
        },
        # Demo User 002's invoices
        "INV-2025-004": {
            "order_id": "ORD-22222",
            "customer_id": "demo-user-002",  # ← Links to jane@example.com
            "date": "2025-01-12",
            "due_date": "2025-02-12",
            "status": "Paid",
            "items": [{"description": "Standing Desk Pro", "qty": 1, "price": 599.99}],
            "subtotal": 599.99,
            "tax": 48.00,
            "total": 647.99,
        },
    }

    payments = {
        # Demo User 001's payments
        "ORD-12345": {
            "customer_id": "demo-user-001",
            "payment_status": "Pending",
            "amount_due": 1295.98,
            "payment_method": "Credit Card (ending 4242)",
        },
        "ORD-67890": {
            "customer_id": "demo-user-001",
            "payment_status": "Completed",
            "amount_paid": 215.99,
            "payment_date": "2025-01-10",
            "transaction_id": "TXN-789456",
            "payment_method": "Credit Card (ending 4242)",
        },
        "ORD-11111": {
            "customer_id": "demo-user-001",
            "payment_status": "Completed",
            "amount_paid": 485.99,
            "payment_date": "2024-12-20",
            "transaction_id": "TXN-111222",
            "payment_method": "Credit Card (ending 4242)",
        },
        # Demo User 002's payments
        "ORD-22222": {
            "customer_id": "demo-user-002",
            "payment_status": "Completed",
            "amount_paid": 647.99,
            "payment_date": "2025-01-12",
            "transaction_id": "TXN-222333",
            "payment_method": "PayPal",
        },
    }

    refund_eligibility = {
        # Demo User 001's refund eligibility
        "ORD-12345": {
            "customer_id": "demo-user-001",
            "eligible": True,
            "reason": "Within 30-day return window",
            "max_refund": 1295.98,
        },
        "ORD-67890": {
            "customer_id": "demo-user-001",
            "eligible": True,
            "reason": "Within 30-day return window",
            "max_refund": 215.99,
        },
        "ORD-11111": {"customer_id": "demo-user-001", "eligible": False, "reason": "Past 30-day return window"},
        # Demo User 002's refund eligibility
        "ORD-22222": {
            "customer_id": "demo-user-002",
            "eligible": True,
            "reason": "Order not yet shipped - can cancel",
            "max_refund": 647.99,
        },
    }

    return {
        "products": products,
        "inventory": inventory,
        "reviews": reviews,
        "orders": orders,
        "invoices": invoices,
        "payments": payments,
        "refund_eligibility": refund_eligibility,
        "users": DEMO_USERS,  # Add demo users to seed data
    }


def add_embeddings_to_products(db, location: str = "us-central1"):
    """Add vector embeddings to products for RAG semantic search.

    Args:
        db: Firestore client
        location: GCP location for Vertex AI
    """
    try:
        from google import genai

        print("\n📊 Adding vector embeddings for RAG search...")

        # Initialize genai client
        genai_client = genai.Client(vertexai=True, location=location)

        # Get all products
        products = list(db.collection("products").stream())
        print(f"   Processing {len(products)} products...")

        for doc in products:
            data = doc.to_dict()

            # Create searchable text from product fields
            search_text_parts = [
                data.get("name", ""),
                data.get("description", ""),
                data.get("category", ""),
                " ".join(data.get("keywords", [])),
            ]
            search_text = " ".join(search_text_parts)

            # Generate embedding
            embeddings_response = genai_client.models.embed_content(model="text-embedding-004", contents=[search_text])
            embedding_vector = embeddings_response.embeddings[0].values

            # Update product with embedding
            doc.reference.update({"embedding": embedding_vector, "search_text": search_text})

        print(f"   ✓ Added embeddings to {len(products)} products")

    except ImportError:
        print("\n⚠️  Skipping embeddings (vertexai not installed)")
        print("   Install with: pip install google-cloud-aiplatform")
    except Exception as e:
        print(f"\n⚠️  Error adding embeddings: {e}")
        print("   You can add embeddings later with: python ops/add_embeddings.py")


def seed_firestore(project_id: str, database_id: str = "(default)", clear: bool = False):
    """Seed Firestore database with sample data.

    This seeds:
    1. Global data: products, inventory, reviews (accessible by all users)
    2. Demo users: Pre-configured accounts with order history
    3. User-specific data: orders, invoices, payments linked to demo users
    4. Vector embeddings for RAG semantic search
    """
    print("=" * 60)
    print("FIRESTORE DATABASE SEEDER")
    print("=" * 60)
    print(f"Project:  {project_id}")
    print(f"Database: {database_id}")

    db = firestore.Client(project=project_id, database=database_id)
    data = get_sample_data()

    if clear:
        print("\n⚠️  Clearing existing data...")
        for collection_name in data.keys():
            docs = db.collection(collection_name).stream()
            for doc in docs:
                doc.reference.delete()
            print(f"   Cleared: {collection_name}")

    print("\n📦 Seeding collections...")
    for collection_name, documents in data.items():
        collection_ref = db.collection(collection_name)
        count = 0
        for doc_id, doc_data in documents.items():
            collection_ref.document(doc_id).set(doc_data)
            count += 1
        print(f"   ✓ {collection_name}: {count} documents")

    # Add vector embeddings for RAG search
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    add_embeddings_to_products(db, location)

    print("\n" + "=" * 60)
    print("✅ DATABASE SEEDING COMPLETE!")
    print("=" * 60)

    # Print demo user credentials for easy testing
    print("\n" + "=" * 60)
    print("DEMO USER ACCOUNTS")
    print("=" * 60)
    print("\nUse these accounts to test with pre-seeded order history:\n")
    print("┌─────────────────────────────────────────────────────────┐")
    print("│ Email: demo@example.com                                 │")
    print("│ Password: demo123                                       │")
    print("│ Tier: Gold | Orders: 3 | Can refund: ORD-12345, ORD-67890│")
    print("├─────────────────────────────────────────────────────────┤")
    print("│ Email: jane@example.com                                 │")
    print("│ Password: jane123                                       │")
    print("│ Tier: Silver | Orders: 1 | Can refund: ORD-22222        │")
    print("└─────────────────────────────────────────────────────────┘")
    print("\n💡 New users who register will start with NO order history.")
    print("   They can browse products but have nothing to track/refund.\n")


def get_demo_user_id(email: str) -> str | None:
    """Get the demo user ID for a given email address.

    This is used by the authentication system to check if a user
    is logging in with a demo account.

    Args:
        email: The email address to check

    Returns:
        The demo user ID if it's a demo account, None otherwise
    """
    return DEMO_USER_EMAILS.get(email.lower())


def is_demo_user(email: str) -> bool:
    """Check if an email belongs to a demo user account.

    Args:
        email: The email address to check

    Returns:
        True if this is a demo account, False otherwise
    """
    return email.lower() in DEMO_USER_EMAILS


def main():
    parser = argparse.ArgumentParser(description="Seed Firestore with demo data")
    parser.add_argument("--project", type=str, required=True, help="GCP Project ID")
    parser.add_argument("--database", type=str, default="(default)", help="Firestore database ID")
    parser.add_argument("--clear", action="store_true", help="Clear existing data first")

    args = parser.parse_args()
    seed_firestore(project_id=args.project, database_id=args.database, clear=args.clear)


if __name__ == "__main__":
    main()
