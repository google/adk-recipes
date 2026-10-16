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

"""Campaign management tools for CRUD operations.

Product-Centric Model:
    Each campaign is tied to a single product at a single store location.
    Example: "Blue Floral Maxi Dress - Westfield Century City"

    This allows:
    - Clear metrics attribution per product per location
    - A/B testing with variations for the same product
    - Same product at different stores = different campaigns
"""

from ..config import CAMPAIGN_CATEGORIES
from ..database.db import get_db_cursor, get_product
from .metrics_shared import compute_rpi


def create_campaign(
    product_id: int,
    store_name: str,
    city: str,
    state: str,
    name: str | None = None,
    description: str | None = None,
    category: str | None = None,
) -> dict:
    """Create a new product-centric ad campaign.

    Each campaign is tied to one product at one store location.
    The campaign name is auto-generated from product and store if not provided.

    The campaign category is a small controlled THEME taxonomy (see
    config.CAMPAIGN_CATEGORIES / the CHECK constraint in app/database/db.py),
    deliberately decoupled from the open-ended product category: pass
    `category` explicitly (validated), or omit it — fashion product
    categories map to suggested themes (dress→summer, top→essentials,
    pants→professional, skirt→formal, outerwear→essentials) and anything
    else defaults to the neutral "always-on" bucket (Phase 8 owner
    decision — replaces the old silent "essentials" fallback).

    Args:
        product_id: The product ID from products table (use list_products to browse)
        store_name: Store or mall name (e.g., "Westfield Century City", "Water Tower Place")
        city: US city for targeting (e.g., "Los Angeles")
        state: US state (e.g., "California" or "CA")
        name: Optional custom campaign name. Auto-generated if not provided.
        description: Optional description. Auto-generated from product metadata if not provided.
        category: Optional campaign theme, one of config.CAMPAIGN_CATEGORIES. Derived from the product when omitted.

    Returns:
        Dictionary with campaign details or error message
    """
    # Validate product exists
    product = get_product(product_id)
    if not product:
        return {
            "status": "error",
            "message": f"Product with ID {product_id} not found. Use list_products() to browse available products.",
        }

    # Resolve the campaign THEME category (decoupled from product category)
    if category is not None:
        category = category.strip().lower()
        if category not in CAMPAIGN_CATEGORIES:
            return {
                "status": "error",
                "message": (
                    f"Invalid campaign category '{category}'. "
                    f"Valid categories: {', '.join(CAMPAIGN_CATEGORIES)}."
                ),
            }
    else:
        product_category = (product.category or "").lower()
        category_mapping = {
            "dress": "summer",
            "top": "essentials",
            "pants": "professional",
            "skirt": "formal",
            "outerwear": "essentials",
        }
        category = category_mapping.get(product_category, "always-on")

    # Auto-generate campaign name if not provided
    if not name:
        # Format: "Product Name - Store Name"
        product_name_title = product.name.replace("-", " ").title()
        name = f"{product_name_title} - {store_name}"

    # Auto-generate description if not provided
    if not description:
        style = product.attributes.get("style")
        color = product.attributes.get("color")
        if style:
            in_color = f" in {color}" if color else ""
            description = (
                f"Campaign for {style}{in_color} at {store_name}, {city}."
            )
        else:
            product_title = product.name.replace("-", " ").title()
            description = (
                f"Campaign for {product_title} at {store_name}, {city}."
            )

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO campaigns (name, description, product_id, store_name, city, state, category, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')
        """,
            (name, description, product_id, store_name, city, state, category),
        )

        campaign_id = cursor.lastrowid

        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        row = cursor.fetchone()

        return {
            "status": "success",
            "message": f"Campaign '{name}' created successfully",
            "campaign": {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "product_id": row["product_id"],
                "product_name": product.name,
                "store_name": row["store_name"],
                "city": row["city"],
                "state": row["state"],
                "category": row["category"],
                "status": row["status"],
                "created_at": row["created_at"],
            },
            "next_steps": [
                f"Generate videos: generate_video_from_product(campaign_id={campaign_id}, product_id={product_id})",
                "Use variations for A/B testing: generate_video_with_variation(...)",
                "Activate videos via Review Agent to go live",
            ],
        }


def list_campaigns(
    status: str | None = None, product_id: int | None = None
) -> dict:
    """List all campaigns, optionally filtered by status or product.

    Args:
        status: Optional filter - one of: draft, active, paused, completed
        product_id: Optional filter by product ID

    Returns:
        Dictionary with list of campaigns and counts
    """
    with get_db_cursor() as cursor:
        # Build query with optional filters
        query = """
            SELECT c.*,
                   p.name as product_name,
                   p.category as product_category,
                   p.color as product_color,
                   COUNT(DISTINCT cv.id) as video_count
            FROM campaigns c
            LEFT JOIN products p ON c.product_id = p.id
            LEFT JOIN campaign_videos cv ON c.id = cv.campaign_id
        """
        conditions = []
        params = []

        if status:
            conditions.append("c.status = ?")
            params.append(status)
        if product_id:
            conditions.append("c.product_id = ?")
            params.append(product_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " GROUP BY c.id ORDER BY c.created_at DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        campaigns = []
        for row in rows:
            campaigns.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "product_color": row["product_color"],
                    "store_name": row["store_name"],
                    "location": f"{row['city']}, {row['state']}",
                    "category": row["category"],
                    "status": row["status"],
                    "video_count": row["video_count"],
                    "created_at": row["created_at"],
                }
            )

        return {
            "status": "success",
            "total_count": len(campaigns),
            "filter": {"status": status, "product_id": product_id},
            "campaigns": campaigns,
        }


def get_campaign(campaign_id: int) -> dict:
    """Get detailed campaign info including product, videos, and metrics summary.

    Args:
        campaign_id: The ID of the campaign to retrieve

    Returns:
        Dictionary with full campaign details including related data
    """
    with get_db_cursor() as cursor:
        # Get campaign with product info
        cursor.execute(
            """
            SELECT c.*, p.name as product_name, p.category as product_category,
                   p.color as product_color, p.style as product_style,
                   p.image_filename as product_image
            FROM campaigns c
            LEFT JOIN products p ON c.product_id = p.id
            WHERE c.id = ?
        """,
            (campaign_id,),
        )
        campaign = cursor.fetchone()

        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Get videos from campaign_videos table (new HITL workflow)
        cursor.execute(
            """
            SELECT id, video_filename, thumbnail_path, variation_name,
                   duration_seconds, status, activated_at, created_at
            FROM campaign_videos
            WHERE campaign_id = ?
            ORDER BY created_at DESC
        """,
            (campaign_id,),
        )
        videos = []
        for row in cursor.fetchall():
            videos.append(
                {
                    "id": row["id"],
                    "video_filename": row["video_filename"],
                    "thumbnail_path": row["thumbnail_path"],
                    "variation_name": row["variation_name"],
                    "duration_seconds": row["duration_seconds"],
                    "status": row["status"],
                    "activated_at": row["activated_at"],
                    "created_at": row["created_at"],
                }
            )

        # Get metrics summary from video_metrics (last 30 days)
        cursor.execute(
            """
            SELECT
                SUM(vm.impressions) as total_impressions,
                AVG(vm.dwell_time_seconds) as avg_dwell_time,
                SUM(vm.circulation) as total_circulation,
                SUM(vm.revenue) as total_revenue
            FROM video_metrics vm
            JOIN campaign_videos cv ON vm.video_id = cv.id
            WHERE cv.campaign_id = ?
            AND vm.metric_date >= date('now', '-30 days')
        """,
            (campaign_id,),
        )
        metrics_row = cursor.fetchone()

        metrics_summary = None
        if metrics_row and metrics_row["total_impressions"]:
            total_impressions = int(metrics_row["total_impressions"])
            total_revenue = round(metrics_row["total_revenue"], 2)
            rpi = compute_rpi(total_revenue, total_impressions)

            metrics_summary = {
                "period": "last_30_days",
                "total_impressions": total_impressions,
                "average_dwell_time": round(metrics_row["avg_dwell_time"], 1),
                "total_circulation": int(metrics_row["total_circulation"]),
                "total_revenue": total_revenue,
                "revenue_per_impression": rpi,
                "revenue_per_1000_impressions": round(rpi * 1000, 2),
            }

        # Build product info
        product_info = None
        if campaign["product_id"]:
            product_info = {
                "id": campaign["product_id"],
                "name": campaign["product_name"],
                "category": campaign["product_category"],
                "color": campaign["product_color"],
                "style": campaign["product_style"],
                "image_filename": campaign["product_image"],
            }

        return {
            "status": "success",
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "description": campaign["description"],
                "store_name": campaign["store_name"],
                "city": campaign["city"],
                "state": campaign["state"],
                "category": campaign["category"],
                "status": campaign["status"],
                "created_at": campaign["created_at"],
                "updated_at": campaign["updated_at"],
            },
            "product": product_info,
            "videos": videos,
            "video_count": len(videos),
            "metrics_summary": metrics_summary,
        }


def update_campaign(
    campaign_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> dict:
    """Update campaign properties.

    Args:
        campaign_id: The ID of the campaign to update
        name: New campaign name (optional)
        description: New description (optional)
        status: New status - one of: draft, active, paused, completed (optional)

    Returns:
        Dictionary with updated campaign details or error message
    """
    # Validate status if provided
    if status:
        valid_statuses = ["draft", "active", "paused", "completed"]
        if status not in valid_statuses:
            return {
                "status": "error",
                "message": f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
            }

    with get_db_cursor() as cursor:
        # Check if campaign exists
        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        if not cursor.fetchone():
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Build update query dynamically
        updates = []
        params = []

        if name:
            updates.append("name = ?")
            params.append(name)
        if description:
            updates.append("description = ?")
            params.append(description)
        if status:
            updates.append("status = ?")
            params.append(status)

        if not updates:
            return {
                "status": "error",
                "message": "No fields to update provided",
            }

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(campaign_id)

        query = f"UPDATE campaigns SET {', '.join(updates)} WHERE id = ?"  # noqa: S608
        cursor.execute(query, params)

        # Get updated campaign
        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        row = cursor.fetchone()

        return {
            "status": "success",
            "message": "Campaign updated successfully",
            "campaign": {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "category": row["category"],
                "city": row["city"],
                "state": row["state"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        }
