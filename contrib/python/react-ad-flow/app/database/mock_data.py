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

"""Mock data population for the Ad Campaign Agent demo.

Uses NEW schema (product-centric model):
- products: 22 pre-loaded fashion products
- campaigns: Each campaign = 1 product + 1 store location
- campaign_videos: Videos with HITL lifecycle status
- video_metrics: Metrics only for activated videos
"""

import json
from datetime import date, datetime, timedelta

from ..demo_data.attribution import screens_for_campaign
from ..demo_data.constants import DEMO_WINDOW_DAYS
from ..demo_data.derive import derive_video_metrics_rows
from ..demo_data.windows import load_attribution_windows
from .db import get_connection, get_demo_anchor_date
from .products_data import PRODUCTS

# =============================================================================
# Product-Centric Campaign Definitions (NEW MODEL)
# =============================================================================
# Each campaign = 1 product + 1 store location
# Campaign name auto-generated: "{Product Name} - {Store Name}"

MOCK_CAMPAIGNS = [
    {
        "product_name": "blue-floral-maxi-dress",
        "store_name": "Westfield Century City",
        "city": "Los Angeles",
        "state": "CA",
        "category": "summer",
        "status": "active",
    },
    {
        "product_name": "elegant-black-cocktail-dress",
        "store_name": "Bloomingdale's 59th Street",
        "city": "New York",
        "state": "NY",
        "category": "formal",
        "status": "active",
    },
    {
        "product_name": "black-high-waist-trousers",
        "store_name": "Water Tower Place",
        "city": "Chicago",
        "state": "IL",
        "category": "professional",
        "status": "active",
    },
    {
        # Changed from emerald-satin-slip-dress (no real videos) to sage-satin-camisole
        "product_name": "sage-satin-camisole",
        "store_name": "The Grove",
        "city": "Los Angeles",
        "state": "CA",
        "category": "essentials",  # 'casual' not in CHECK constraint; using 'essentials'
        "status": "active",
    },
]

# =============================================================================
# REAL demo videos (generated/ prefix in whatever GCS bucket is configured)
# =============================================================================
# These are actual Omni-generated videos that exist in the bucket.
# Each campaign gets multiple real videos with thumbnails.

REAL_VIDEOS = {
    "blue-floral-maxi-dress": [
        {
            "filename": "blue-floral-maxi-dress-122025-asian-beach-romantic.mp4",
            "thumbnail": "blue-floral-maxi-dress-122025-asian-beach-romantic-thumbnail.png",
            "variation": {
                "model_ethnicity": "asian",
                "setting": "beach",
                "mood": "romantic",
                "time_of_day": "day",
            },
        },
        {
            "filename": "blue-floral-maxi-dress-122025-european-urban-bold.mp4",
            "thumbnail": "blue-floral-maxi-dress-122025-european-urban-bold-thumbnail.png",
            "variation": {
                "model_ethnicity": "european",
                "setting": "urban",
                "mood": "bold",
                "time_of_day": "day",
            },
        },
        {
            "filename": "blue-floral-maxi-dress-122225-latina-beach-romantic.mp4",
            "thumbnail": "blue-floral-maxi-dress-122225-latina-beach-romantic-thumbnail.png",
            "variation": {
                "model_ethnicity": "latina",
                "setting": "beach",
                "mood": "romantic",
                "time_of_day": "day",
            },
        },
    ],
    "elegant-black-cocktail-dress": [
        {
            "filename": "elegant-black-cocktail-dress-122225-diverse-rooftop-sophisticated.mp4",
            "thumbnail": "elegant-black-cocktail-dress-122225-diverse-rooftop-sophisticated-thumbnail.png",
            "variation": {
                "model_ethnicity": "diverse",
                "setting": "rooftop",
                "mood": "sophisticated",
                "time_of_day": "day",
            },
        },
    ],
    "black-high-waist-trousers": [
        {
            "filename": "black-high-waist-trousers-122025-african-studio-sophisticated.mp4",
            "thumbnail": "black-high-waist-trousers-122025-african-studio-sophisticated-thumbnail.png",
            "variation": {
                "model_ethnicity": "african",
                "setting": "studio",
                "mood": "sophisticated",
                "time_of_day": "day",
            },
        },
        {
            "filename": "black-high-waist-trousers-122025-asian-cafe-sophisticated.mp4",
            "thumbnail": "black-high-waist-trousers-122025-asian-cafe-sophisticated-thumbnail.png",
            "variation": {
                "model_ethnicity": "asian",
                "setting": "cafe",
                "mood": "sophisticated",
                "time_of_day": "day",
            },
        },
        {
            "filename": "black-high-waist-trousers-122025-latina-rooftop-bold.mp4",
            "thumbnail": "black-high-waist-trousers-122025-latina-rooftop-bold-thumbnail.png",
            "variation": {
                "model_ethnicity": "latina",
                "setting": "rooftop",
                "mood": "bold",
                "time_of_day": "day",
            },
        },
    ],
    "sage-satin-camisole": [
        {
            "filename": "sage-satin-camisole-122225-african-studio-bold.mp4",
            "thumbnail": "sage-satin-camisole-122225-african-studio-bold-thumbnail.png",
            "variation": {
                "model_ethnicity": "african",
                "setting": "studio",
                "mood": "bold",
                "time_of_day": "day",
            },
        },
        {
            "filename": "sage-satin-camisole-122225-asian-cafe-sophisticated.mp4",
            "thumbnail": "sage-satin-camisole-122225-asian-cafe-sophisticated-thumbnail.png",
            "variation": {
                "model_ethnicity": "asian",
                "setting": "cafe",
                "mood": "sophisticated",
                "time_of_day": "day",
            },
        },
        {
            "filename": "sage-satin-camisole-122225-european-urban-elegant.mp4",
            "thumbnail": "sage-satin-camisole-122225-european-urban-elegant-thumbnail.png",
            "variation": {
                "model_ethnicity": "european",
                "setting": "urban",
                "mood": "elegant",
                "time_of_day": "day",
            },
        },
    ],
}

# Legacy variation presets (for new video generation)
MOCK_VARIATIONS = [
    {
        "model_ethnicity": "asian",
        "setting": "beach",
        "mood": "romantic",
        "time_of_day": "golden-hour",
    },
    {
        "model_ethnicity": "european",
        "setting": "urban",
        "mood": "sophisticated",
        "time_of_day": "day",
    },
    {
        "model_ethnicity": "african",
        "setting": "studio",
        "mood": "bold",
        "time_of_day": "day",
    },
    {
        "model_ethnicity": "latina",
        "setting": "rooftop",
        "mood": "romantic",
        "time_of_day": "sunset",
    },
]


def _get_product_by_name(product_name: str) -> dict:
    """Find a product by its hyphenated name."""
    for product in PRODUCTS:
        if product["name"] == product_name:
            return product
    return None


def _generate_campaign_name(product_name: str, store_name: str) -> str:
    """Generate campaign name from product and store.

    Example: "blue-floral-maxi-dress" + "Westfield Century City"
             -> "Blue Floral Maxi Dress - Westfield Century City"
    """
    # Convert hyphenated name to title case
    product_title = product_name.replace("-", " ").title()
    return f"{product_title} - {store_name}"


def populate_mock_data() -> dict:
    """Populate the database with mock data using NEW schema.

    Creates:
    - 22 products (from products_data.py)
    - 4 product-centric campaigns
    - 1 activated video per campaign
    - 30 days of metrics per activated video

    On repeat calls, regenerates metrics deterministically for all existing
    activated videos (idempotent on video/campaign lifecycle).

    Returns:
        Dictionary with counts of created records
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Check if products/campaigns already exist
    cursor.execute("SELECT COUNT(*) FROM campaigns")
    campaign_count = cursor.fetchone()[0]

    # If no campaigns, create full structure; otherwise just regenerate metrics
    if campaign_count == 0:
        products_created = 0
        campaigns_created = 0
        videos_created = 0

        # Step 1: Insert all 22 products
        # Use INSERT OR IGNORE to handle multi-process race conditions (Agent Engine)
        for product in PRODUCTS:
            cursor.execute(
                """
                INSERT OR IGNORE INTO products (name, category, style, color, fabric, details, occasion, image_filename, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    product["name"],
                    product["category"],
                    product.get("style", ""),
                    product.get("color", ""),
                    product.get("fabric", ""),
                    product.get("details", ""),
                    product.get("occasion", ""),
                    product["image_filename"],
                    json.dumps(product),
                ),
            )
            products_created += 1

        # Step 2: Create product-centric campaigns
        for i, camp_data in enumerate(MOCK_CAMPAIGNS):
            # Find the product
            product = _get_product_by_name(camp_data["product_name"])
            if not product:
                continue

            # Get the product ID (1-indexed based on insertion order)
            cursor.execute(
                "SELECT id FROM products WHERE name = ?", (product["name"],)
            )
            product_row = cursor.fetchone()
            if not product_row:
                continue
            product_id = product_row[0]

            # Generate campaign name
            campaign_name = _generate_campaign_name(
                product["name"], camp_data["store_name"]
            )

            # Insert campaign with product_id and store_name
            # Use INSERT OR IGNORE for multi-process safety
            cursor.execute(
                """
                INSERT OR IGNORE INTO campaigns (name, description, product_id, store_name, city, state, category, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    campaign_name,
                    f"Campaign for {product['name']} at {camp_data['store_name']}",
                    product_id,
                    camp_data["store_name"],
                    camp_data["city"],
                    camp_data["state"],
                    camp_data["category"],
                    camp_data["status"],
                ),
            )

            # Fetch the actual campaign_id (lastrowid is unreliable with INSERT OR IGNORE)
            cursor.execute(
                "SELECT id FROM campaigns WHERE name = ?", (campaign_name,)
            )
            campaign_row = cursor.fetchone()
            if not campaign_row:
                continue
            campaign_id = campaign_row[0]
            campaigns_created += 1

            # Step 3: Create activated videos using REAL GCS video files
            if camp_data["status"] == "active":
                product_name = product["name"]
                real_video_list = REAL_VIDEOS.get(product_name, [])

                # If no real videos exist for this product, create one placeholder
                if not real_video_list:
                    variation = MOCK_VARIATIONS[i % len(MOCK_VARIATIONS)]
                    variation_name = f"{variation['model_ethnicity']}-{variation['setting']}-{variation['time_of_day']}"
                    date_str = datetime.now().strftime("%m%d%y")
                    real_video_list = [
                        {
                            "filename": f"{product_name}-{date_str}-{variation_name}.mp4",
                            "thumbnail": f"{product_name}-{date_str}-{variation_name}-thumbnail.png",
                            "variation": variation,
                        }
                    ]

                # Insert ALL real videos for this campaign
                for video_data in real_video_list:
                    variation = video_data["variation"]
                    variation_name = f"{variation['model_ethnicity']}-{variation['setting']}-{variation['mood']}"

                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO campaign_videos
                        (campaign_id, product_id, video_filename, thumbnail_path,
                         scene_prompt, video_prompt, pipeline_type,
                         variation_name, variation_params, duration_seconds, aspect_ratio,
                         status, activated_at, activated_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            campaign_id,
                            product_id,
                            video_data["filename"],
                            video_data["thumbnail"],
                            f"Scene: {variation['model_ethnicity']} model wearing {product_name} in {variation['setting']}",
                            f"Cinematic video of model in {product_name}, {variation['mood']} mood",
                            "two-stage",
                            variation_name,
                            json.dumps(variation),
                            8,
                            "9:16",
                            "activated",  # Pre-activated for demo
                            datetime.now().isoformat(),
                            "mock_data",
                        ),
                    )

                    # Fetch the actual video_id (lastrowid is unreliable with INSERT OR IGNORE)
                    cursor.execute(
                        "SELECT id FROM campaign_videos WHERE video_filename = ?",
                        (video_data["filename"],),
                    )
                    video_row = cursor.fetchone()
                    if not video_row:
                        continue
                    videos_created += 1
    else:
        products_created = 0
        campaigns_created = 0
        videos_created = 0

    # Step 4: Always regenerate deterministic metrics for all active videos
    # on the single anchor window [anchor-29, anchor]
    metrics_created = 0
    anchor = date.fromisoformat(get_demo_anchor_date(cursor))
    window_start = anchor - timedelta(days=DEMO_WINDOW_DAYS - 1)

    # Get all active campaigns with their videos
    cursor.execute("""
        SELECT DISTINCT c.id FROM campaigns c
        WHERE c.status = 'active'
    """)
    active_campaigns = [row[0] for row in cursor.fetchall()]

    for campaign_id in active_campaigns:
        # Get all activated videos for this campaign
        cursor.execute(
            """
            SELECT id FROM campaign_videos
            WHERE campaign_id = ? AND status = 'activated'
        """,
            (campaign_id,),
        )
        campaign_video_ids = [row[0] for row in cursor.fetchall()]

        if not campaign_video_ids:
            continue

        # Phase 10: pre-activated demo videos get attribution windows (there
        # is no activation moment on this path — the open window from
        # window_start IS the "has been live for 30 days" fiction).
        active_from_iso = datetime.combine(
            window_start, datetime.min.time()
        ).isoformat()
        for video_id in campaign_video_ids:
            for screen_id in screens_for_campaign(campaign_id):
                cursor.execute(
                    """
                    SELECT 1 FROM video_attribution
                    WHERE video_id = ? AND screen_id = ? AND active_to IS NULL
                """,
                    (video_id, screen_id),
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """
                    INSERT INTO video_attribution
                    (video_id, ad_campaign_id, screen_id, active_from, active_to)
                    VALUES (?, ?, ?, ?, NULL)
                """,
                    (video_id, campaign_id, screen_id, active_from_iso),
                )

        # Delete old metrics for these videos (to allow deterministic regeneration)
        for video_id in campaign_video_ids:
            cursor.execute(
                "DELETE FROM video_metrics WHERE video_id = ?", (video_id,)
            )

        # Generate deterministic metrics through the stored windows
        # Shared loader in app/demo_data — legal layering (no app.tools import).
        windows = load_attribution_windows(cursor, campaign_video_ids)
        for row in derive_video_metrics_rows(
            campaign_id,
            campaign_video_ids,
            window_start,
            anchor,
            windows=windows,
        ):
            cursor.execute(
                """
                INSERT OR IGNORE INTO video_metrics
                (video_id, metric_date, impressions, dwell_time_seconds, circulation, revenue)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    row["video_id"],
                    row["metric_date"],
                    row["impressions"],
                    row["dwell_time_seconds"],
                    row["circulation"],
                    row["revenue"],
                ),
            )
            metrics_created += 1

    conn.commit()
    conn.close()

    return {
        "status": "success",
        "products_created": products_created,
        "campaigns_created": campaigns_created,
        "videos_created": videos_created,
        "metrics_created": metrics_created,
    }


def seed_demo_data() -> dict:
    """Seed demo data per config.DEMO_DATASET (Phase 15 gated seeding).

    fashion: the full demo — 22 fashion products, the retail core test set,
    and 4 demo campaigns with activated videos and metrics (all idempotent).
    none: seed nothing — schema-only empty catalog for from-scratch onboarding.

    Reads config at call time so tests can monkeypatch app.config.DEMO_DATASET.
    """
    from .. import config
    from .db import populate_products, populate_retail_test_products

    if config.DEMO_DATASET is config.DemoDataset.NONE:
        print("[DB] DEMO_DATASET=none — skipping demo seeding (empty catalog)")
        return {"seeded": False}
    populate_products()
    populate_retail_test_products()
    counts = populate_mock_data()
    return {"seeded": True, **(counts or {})}
