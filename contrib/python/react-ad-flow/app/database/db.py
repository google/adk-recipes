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

"""SQLite database setup and connection management."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from .. import config
from ..models.product import Product


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory enabled."""
    # Resolve at call time so tests can patch app.config.DB_PATH (see tests/conftest.py)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db_cursor():
    """Context manager for database operations."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def init_database() -> None:
    """Initialize the database schema.

    Creates tables for:
    - campaigns: Campaign metadata and location targeting
    - products: Product catalog (22 pre-generated products)
    - campaign_products: Junction table for campaign-product relationships
    - campaign_images: Seed images associated with campaigns (legacy)
    - campaign_videos: Generated video ads with HITL status
    - campaign_ads: Generated video ads (legacy alias)
    - video_metrics: Daily performance metrics (only for activated videos)
    - campaign_metrics: Daily performance metrics (legacy alias)
    - video_attribution: Playout attribution windows (Phase 10, open = active_to IS NULL)
    - demo_meta: Key-value store for demo-mode state (anchor date)
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Create campaigns table (product-centric: 1 campaign = 1 product + 1 store location)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            product_id INTEGER,
            store_name TEXT,
            city TEXT NOT NULL,
            state TEXT NOT NULL,
            category TEXT CHECK(category IN ('summer', 'formal', 'professional', 'essentials', 'holiday', 'always-on')),
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'active', 'paused', 'completed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
        )
    """)

    # Create products table (source of truth - 22 products)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT,
            style TEXT,
            color TEXT,
            fabric TEXT,
            details TEXT,
            occasion TEXT,
            image_filename TEXT NOT NULL,
            gcs_path TEXT,
            local_path TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create campaign_products junction table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            UNIQUE(campaign_id, product_id)
        )
    """)

    # Create campaign_videos table (new video schema with HITL)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            product_id INTEGER,
            video_filename TEXT NOT NULL UNIQUE,
            gcs_path TEXT,
            local_path TEXT,
            thumbnail_path TEXT,
            scene_prompt TEXT,
            video_prompt TEXT,
            pipeline_type TEXT DEFAULT 'two-stage',
            variation_name TEXT,
            variation_params TEXT,
            duration_seconds INTEGER DEFAULT 8,
            aspect_ratio TEXT DEFAULT '9:16',
            status TEXT DEFAULT 'generated' CHECK(status IN ('generating', 'generated', 'activated', 'paused', 'archived')),
            activated_at TIMESTAMP,
            activated_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            generation_time_seconds INTEGER,
            source_video_id INTEGER,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
            FOREIGN KEY (source_video_id) REFERENCES campaign_videos(id) ON DELETE SET NULL
        )
    """)

    # Create video_metrics table (only for activated videos)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS video_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            metric_date DATE NOT NULL,
            impressions INTEGER DEFAULT 0,
            dwell_time_seconds REAL DEFAULT 0.0,
            circulation INTEGER DEFAULT 0,
            revenue REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (video_id) REFERENCES campaign_videos(id) ON DELETE CASCADE,
            UNIQUE(video_id, metric_date)
        )
    """)

    # Playout-attribution windows (Phase 10): which video was live on which
    # screen, from when to when. Open window = active_to IS NULL. Written by
    # the HITL dual-write bridge (activate/pause/archive) and the seed-time
    # bulk path; read by the ad-play join. Ported from the donor repo's
    # video_attribution with store_id->screen_id, campaign_id->ad_campaign_id.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS video_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            ad_campaign_id INTEGER NOT NULL,
            screen_id INTEGER NOT NULL,
            active_from TIMESTAMP NOT NULL,
            active_to TIMESTAMP,
            FOREIGN KEY (video_id) REFERENCES campaign_videos(id) ON DELETE CASCADE
        )
    """)

    # Key-value store for demo-mode state (e.g. the demo anchor date that
    # fixes the deterministic generation window — see app/demo_data/).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS demo_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Legacy tables for backward compatibility
    # Create campaign_images table (legacy)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            image_type TEXT DEFAULT 'seed' CHECK(image_type IN ('seed', 'reference')),
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        )
    """)

    # Create campaign_ads table (legacy)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            image_id INTEGER,
            video_path TEXT NOT NULL,
            prompt_used TEXT,
            duration_seconds INTEGER DEFAULT 5,
            status TEXT DEFAULT 'completed' CHECK(status IN ('pending', 'generating', 'completed', 'failed')),
            video_properties TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES campaign_images(id) ON DELETE SET NULL
        )
    """)

    # Create campaign_metrics table (legacy - In-Store Retail Media metrics)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            ad_id INTEGER,
            date DATE NOT NULL,
            impressions INTEGER DEFAULT 0,
            dwell_time REAL DEFAULT 0.0,
            circulation INTEGER DEFAULT 0,
            revenue REAL DEFAULT 0.0,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY (ad_id) REFERENCES campaign_ads(id) ON DELETE SET NULL
        )
    """)

    # Create base indexes (columns that always exist)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_products_campaign ON campaign_products(campaign_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_products_product ON campaign_products(product_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_videos_campaign ON campaign_videos(campaign_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_videos_product ON campaign_videos(product_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_videos_status ON campaign_videos(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_metrics_video ON video_metrics(video_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_metrics_date ON video_metrics(metric_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_attribution_video ON video_attribution(video_id)"
    )
    # Legacy indexes
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_images_campaign ON campaign_images(campaign_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_ads_campaign ON campaign_ads(campaign_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_metrics_campaign ON campaign_metrics(campaign_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_metrics_date ON campaign_metrics(date)"
    )

    conn.commit()
    conn.close()

    # Run migrations for existing databases (adds new columns)
    run_migrations()

    # Create indexes for columns added by migrations
    create_migration_indexes()


def create_migration_indexes() -> None:
    """Create indexes for columns added by migrations.

    This runs after migrations to ensure columns exist before indexing.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Check if product_id column exists before creating index
    cursor.execute("PRAGMA table_info(campaigns)")
    campaign_columns = [column[1] for column in cursor.fetchall()]

    if "product_id" in campaign_columns:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_campaigns_product ON campaigns(product_id)"
        )
    if "store_name" in campaign_columns:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_campaigns_store ON campaigns(store_name)"
        )

    conn.commit()
    conn.close()


def run_migrations() -> None:
    """Run database migrations for schema updates.

    This function handles adding new columns to existing databases.
    It checks if columns exist before attempting to add them.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Migration 1: Add video_properties column to campaign_ads
    cursor.execute("PRAGMA table_info(campaign_ads)")
    columns = [column[1] for column in cursor.fetchall()]

    if "video_properties" not in columns:
        print(
            "[DB Migration] Adding video_properties column to campaign_ads..."
        )
        cursor.execute(
            "ALTER TABLE campaign_ads ADD COLUMN video_properties TEXT"
        )
        conn.commit()
        print("[DB Migration] video_properties column added successfully.")

    # Migration 2: Add product_id and store_name to campaigns (product-centric model)
    cursor.execute("PRAGMA table_info(campaigns)")
    campaign_columns = [column[1] for column in cursor.fetchall()]

    if "product_id" not in campaign_columns:
        print("[DB Migration] Adding product_id column to campaigns...")
        cursor.execute(
            "ALTER TABLE campaigns ADD COLUMN product_id INTEGER REFERENCES products(id)"
        )
        conn.commit()
        print("[DB Migration] product_id column added successfully.")

    if "store_name" not in campaign_columns:
        print("[DB Migration] Adding store_name column to campaigns...")
        cursor.execute("ALTER TABLE campaigns ADD COLUMN store_name TEXT")
        conn.commit()
        print("[DB Migration] store_name column added successfully.")

    # Migration 2: Check if campaign_metrics needs retail media migration
    # This checks if old columns exist - if so, run migrate_metrics_schema.py
    cursor.execute("PRAGMA table_info(campaign_metrics)")
    metrics_columns = [column[1] for column in cursor.fetchall()]

    if "views" in metrics_columns or "clicks" in metrics_columns:
        print(
            "[DB Migration] WARNING: campaign_metrics has old digital video columns."
        )
        print("[DB Migration] Run: python -m scripts.migrate_metrics_schema")
        print("[DB Migration] to migrate to in-store retail media metrics.")

    # Check if new columns exist (for fresh databases or after migration)
    if "dwell_time" not in metrics_columns and "views" not in metrics_columns:
        # This is a fresh database with new schema - add columns
        print("[DB Migration] Adding dwell_time column to campaign_metrics...")
        cursor.execute(
            "ALTER TABLE campaign_metrics ADD COLUMN dwell_time REAL DEFAULT 0.0"
        )
        print("[DB Migration] Adding circulation column to campaign_metrics...")
        cursor.execute(
            "ALTER TABLE campaign_metrics ADD COLUMN circulation INTEGER DEFAULT 0"
        )
        conn.commit()
        print("[DB Migration] Retail media columns added successfully.")

    # Migration N: Add source_video_id to campaign_videos (Phase 14c lineage)
    cursor.execute("PRAGMA table_info(campaign_videos)")
    video_columns = [column[1] for column in cursor.fetchall()]

    if "source_video_id" not in video_columns:
        print(
            "[DB Migration] Adding source_video_id column to campaign_videos..."
        )
        cursor.execute(
            "ALTER TABLE campaign_videos ADD COLUMN source_video_id INTEGER "
            "REFERENCES campaign_videos(id) ON DELETE SET NULL"
        )
        conn.commit()
        print("[DB Migration] source_video_id column added successfully.")

    conn.close()


def reset_database() -> None:
    """Drop all tables and reinitialize the database."""
    conn = get_connection()
    cursor = conn.cursor()

    # Drop new tables
    cursor.execute("DROP TABLE IF EXISTS demo_meta")
    cursor.execute("DROP TABLE IF EXISTS video_attribution")
    cursor.execute("DROP TABLE IF EXISTS video_metrics")
    cursor.execute("DROP TABLE IF EXISTS campaign_videos")
    cursor.execute("DROP TABLE IF EXISTS campaign_products")
    cursor.execute("DROP TABLE IF EXISTS products")

    # Drop legacy tables
    cursor.execute("DROP TABLE IF EXISTS campaign_metrics")
    cursor.execute("DROP TABLE IF EXISTS campaign_ads")
    cursor.execute("DROP TABLE IF EXISTS campaign_images")
    cursor.execute("DROP TABLE IF EXISTS campaigns")

    conn.commit()
    conn.close()

    init_database()


def populate_products() -> None:
    """Populate the products table with 22 pre-generated products.

    Products are loaded from products_data.py which contains metadata
    parsed from scripts/products/*.txt files.
    """
    import json

    from .products_data import PRODUCTS

    conn = get_connection()
    cursor = conn.cursor()

    # Check if products already exist
    cursor.execute("SELECT COUNT(*) FROM products")
    count = cursor.fetchone()[0]
    if count > 0:
        conn.close()
        return  # Products already populated

    for product in PRODUCTS:
        cursor.execute(
            """
            INSERT OR IGNORE INTO products
            (name, category, style, color, fabric, details, occasion,
             image_filename, local_path, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                product.get("name"),
                product.get("category"),
                product.get("style"),
                product.get("color"),
                product.get("fabric"),
                product.get("details"),
                product.get("occasion"),
                product.get("image_filename"),
                product.get("local_path"),
                json.dumps(product),
            ),
        )

    conn.commit()
    conn.close()
    print(f"[DB] Populated {len(PRODUCTS)} products")


def insert_product(product: Product) -> Product:
    """Insert a new product through the typed write path (Phase 8).

    The db-layer substrate for self-service onboarding ("run MY product
    through it"): Phase 15's agent tools (create_product / import /
    generate_product_image) wrap this. The referenced image file does NOT
    need to exist yet — image_filename is a reference resolved later by
    vendor upload or image generation.

    Returns:
        The stored Product, re-read via get_product() so the caller sees
        exactly what any later retrieval will see (id populated).

    Raises:
        sqlite3.IntegrityError: if a product with this name already exists.
    """
    row = product.to_row()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO products
            (name, category, style, color, fabric, occasion, details,
             image_filename, gcs_path, local_path, metadata)
            VALUES (:name, :category, :style, :color, :fabric, :occasion,
                    :details, :image_filename, :gcs_path, :local_path, :metadata)
        """,
            row,
        )
        product_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return get_product(product_id)


def populate_retail_test_products() -> None:
    """Seed the multi-vertical retail core test set (Phase 8).

    Attributes-first products across five verticals proving the schema is
    vertical-agnostic. Additive and idempotent (INSERT OR IGNORE on the
    UNIQUE name); the fashion demo catalog is untouched. Writes go through
    Product.to_row() — the same write path Phase 15's create_product uses.
    """
    from .retail_products_data import RETAIL_TEST_PRODUCTS

    conn = get_connection()
    cursor = conn.cursor()
    try:
        for entry in RETAIL_TEST_PRODUCTS:
            row = Product(**entry).to_row()
            cursor.execute(
                """
                INSERT OR IGNORE INTO products
                (name, category, style, color, fabric, occasion, details,
                 image_filename, gcs_path, local_path, metadata)
                VALUES (:name, :category, :style, :color, :fabric, :occasion,
                        :details, :image_filename, :gcs_path, :local_path, :metadata)
            """,
                row,
            )
        conn.commit()
    finally:
        conn.close()


def get_product(product_id: int) -> Product | None:
    """Get a product by ID as a typed Product (metadata parsed into attributes).

    Args:
        product_id: The product ID

    Returns:
        Product or None if not found
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return Product.from_row(dict(row))
    return None


def get_product_by_name(name: str) -> Product | None:
    """Get a product by name as a typed Product (metadata parsed into attributes).

    Args:
        name: The product name (e.g., 'emerald-satin-slip-dress')

    Returns:
        Product or None if not found
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE name = ?", (name,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return Product.from_row(dict(row))
    return None


def list_products(category: str | None = None) -> list[Product]:
    """List all products, optionally filtered by category.

    Args:
        category: Optional category filter

    Returns:
        List of typed Products
    """
    conn = get_connection()
    cursor = conn.cursor()

    if category:
        cursor.execute(
            "SELECT * FROM products WHERE category = ? ORDER BY name",
            (category,),
        )
    else:
        cursor.execute("SELECT * FROM products ORDER BY name")

    rows = cursor.fetchall()
    conn.close()

    return [Product.from_row(dict(row)) for row in rows]


DEMO_ANCHOR_KEY = "demo_anchor_date"


def get_demo_anchor_date(cursor) -> str:
    """The demo anchor date (ISO), created as today's UTC date on first read.

    All deterministic demo generation windows are anchored here (UTC day
    buckets by convention), so seed-time and activation-time data share one
    windowing rule."""
    cursor.execute(
        "SELECT value FROM demo_meta WHERE key = ?", (DEMO_ANCHOR_KEY,)
    )
    row = cursor.fetchone()
    if row:
        return row["value"]
    today = datetime.now(UTC).date().isoformat()
    cursor.execute(
        "INSERT OR IGNORE INTO demo_meta (key, value) VALUES (?, ?)",
        (DEMO_ANCHOR_KEY, today),
    )
    return today


def set_demo_anchor_date(cursor, iso_date: str) -> None:
    """Overwrite the demo anchor date (ISO string)."""
    cursor.execute(
        """
        INSERT INTO demo_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (DEMO_ANCHOR_KEY, iso_date),
    )
