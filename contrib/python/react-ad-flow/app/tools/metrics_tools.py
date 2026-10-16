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

"""Metrics and analytics tools for campaign performance."""

import json
import time
from io import BytesIO

from google import genai
from google.adk.tools import ToolContext
from google.genai import types
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ..config import IMAGE_GENERATION
from ..database.db import get_db_cursor
from .metrics_shared import compute_rpi, compute_weighted_average

# =============================================================================
# Chart Prompt Templates (Anti-Hallucination)
# =============================================================================
# These templates ensure AI generates charts with ONLY the provided data values.
# Explicit "DO NOT invent" clauses prevent fabricated data points.

CHART_TEMPLATES = {
    "trendline": """Create a professional line chart visualization.

=== REQUIRED LAYOUT (16:9 aspect ratio) ===
1. CHART AREA (80% of image)
   - Title: "{campaign_name} - {metric_display} Trend"
   - X-Axis: Dates from {start_date} to {end_date}
   - Y-Axis: {metric_display} ({value_format})
   - Subtitle: "Last {days} days"

2. DATA LINE
   - Smooth curve connecting exact data points
   - Dark blue line (#2563eb) with gradient fill below
   - Data points (MUST use these exact values):
{data_points_formatted}

3. STATISTICS PANEL (bottom right corner)
   - Average: {avg_val}
   - Peak: {max_val}
   - Low: {min_val}
   - Trend: {trend}

=== STYLE REQUIREMENTS ===
- Modern dashboard aesthetic
- White/light gray background
- Sans-serif font (Roboto/Inter style)
- Subtle grid lines
- Small circular data point markers

=== STRICT DATA RULES ===
- Use ONLY the {num_points} data values provided above
- Do NOT invent, estimate, or interpolate any numbers
- The line must pass through EXACTLY these data points
- 16:9 aspect ratio, high resolution
- Professional business visualization quality
""",
    "bar_chart": """Create a professional bar chart visualization.

=== REQUIRED LAYOUT (16:9 aspect ratio) ===
1. CHART AREA
   - Title: "{campaign_name} - Weekly {metric_display}"
   - X-Axis: Week labels
   - Y-Axis: {metric_display}

2. BAR DATA (MUST use these exact values):
{weekly_data_formatted}

3. STYLE
   - Modern gradient bars (blue to purple)
   - Value labels above each bar
   - White background
   - Subtle shadows for depth

=== STRICT DATA RULES ===
- Create exactly {num_bars} bars with the values above
- Do NOT add extra weeks or estimate values
- Each bar height must represent its exact value
- 16:9 aspect ratio
""",
    "comparison": """Create a multi-metric KPI dashboard card.

=== REQUIRED LAYOUT (16:9 aspect ratio) ===
1. HEADER
   - Campaign: "{campaign_name}"
   - Period: Last {days} days
   - Context: In-Store Retail Media

2. METRIC BOXES (2x2 grid) - EXACT VALUES:
   - Box 1 (PRIMARY - highlight in blue):
     Label: "Revenue Per Impression (RPI)"
     Value: ${rpi:.4f}
   - Box 2:
     Label: "Total Impressions"
     Value: {impressions:,}
   - Box 3:
     Label: "Avg Dwell Time"
     Value: {dwell_time:.1f}s
   - Box 4:
     Label: "Total Circulation"
     Value: {circulation:,}

3. STYLE
   - Clean dashboard card layout
   - RPI box prominently highlighted (larger, blue accent)
   - White background with subtle shadows
   - Large, bold numbers
   - Trend arrows where applicable

=== STRICT DATA RULES ===
- Display ONLY the 4 metric values provided above
- Do NOT calculate or invent any other numbers
- RPI is the PRIMARY metric - make it most prominent
- 16:9 aspect ratio
""",
    "infographic": """Create a comprehensive visual infographic.

=== REQUIRED LAYOUT (16:9 aspect ratio) ===
1. HEADER SECTION
   - Campaign: "{campaign_name}"
   - Period: Last {days} days - {trend} trend
   - Context: In-Store Retail Media Analytics

2. PRIMARY METRIC (prominent center-left)
   - {metric_display}: {primary_value}
   - Range: {value_format}

3. PERFORMANCE METRICS (right panel)
   - RPI: ${rpi:.4f} (PRIMARY KPI - highlight)
   - Impressions: {impressions:,}
   - Dwell Time: {dwell_time:.1f}s
   - Circulation: {circulation:,}

4. MINI TREND CHART (bottom)
   - {num_points} data points showing {metric_display}
   - Indicate {trend} direction with arrow

5. STYLE
   - Magazine-quality data visualization
   - Professional blues, teals, and accents
   - Mix of icons, numbers, and mini charts
   - Modern flat design with subtle gradients

=== STRICT DATA RULES ===
- Use ONLY the exact values provided above
- Do NOT invent statistics or additional metrics
- All numbers must match the data provided exactly
- 16:9 aspect ratio, presentation quality
""",
}


def get_campaign_metrics(campaign_id: int, days: int = 30) -> dict:
    """Get performance metrics for a campaign.

    Returns daily and aggregated in-store retail media metrics including
    impressions, dwell time, circulation, and revenue per impression (RPI).

    Uses NEW schema: video_metrics + campaign_videos (HITL workflow).
    Metrics only exist for activated videos.

    Args:
        campaign_id: The ID of the campaign
        days: Number of days to retrieve (default: 30)

    Returns:
        Dictionary with daily metrics and aggregated totals
    """
    print(
        f"[DEBUG get_campaign_metrics] Starting for campaign_id={campaign_id}, days={days}"
    )
    with get_db_cursor() as cursor:
        # Verify campaign exists
        cursor.execute(
            "SELECT id, name, status FROM campaigns WHERE id = ?",
            (campaign_id,),
        )
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Get daily metrics from video_metrics (NEW schema)
        # Only includes activated videos
        cursor.execute(
            """
            SELECT
                vm.metric_date as date,
                SUM(vm.impressions) as impressions,
                AVG(vm.dwell_time_seconds) as avg_dwell_time,
                SUM(vm.circulation) as circulation,
                SUM(vm.revenue) as revenue
            FROM video_metrics vm
            JOIN campaign_videos cv ON vm.video_id = cv.id
            WHERE cv.campaign_id = ?
              AND cv.status = 'activated'
              AND vm.metric_date >= date('now', ?)
            GROUP BY vm.metric_date
            ORDER BY vm.metric_date DESC
        """,
            (campaign_id, f"-{days} days"),
        )

        daily_metrics = []
        for row in cursor.fetchall():
            impressions = int(row["impressions"]) if row["impressions"] else 0
            revenue = round(row["revenue"], 2) if row["revenue"] else 0
            rpi = compute_rpi(revenue, impressions)

            daily_metrics.append(
                {
                    "date": row["date"],
                    "impressions": impressions,
                    "dwell_time": round(row["avg_dwell_time"], 1)
                    if row["avg_dwell_time"]
                    else 0,
                    "circulation": int(row["circulation"])
                    if row["circulation"]
                    else 0,
                    "revenue": revenue,
                    "revenue_per_impression": rpi,
                }
            )

        # Get aggregated totals from video_metrics (NEW schema)
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
              AND cv.status = 'activated'
              AND vm.metric_date >= date('now', ?)
        """,
            (campaign_id, f"-{days} days"),
        )

        totals = cursor.fetchone()

        summary = None
        if totals and totals["total_impressions"]:
            total_impressions = int(totals["total_impressions"])
            total_revenue = (
                round(totals["total_revenue"], 2)
                if totals["total_revenue"]
                else 0
            )
            # RPI is THE key metric for retail media
            rpi = compute_rpi(total_revenue, total_impressions)

            summary = {
                "total_impressions": total_impressions,
                "average_dwell_time": round(totals["avg_dwell_time"], 1)
                if totals["avg_dwell_time"]
                else 0,
                "total_circulation": int(totals["total_circulation"])
                if totals["total_circulation"]
                else 0,
                "total_revenue": total_revenue,
                "revenue_per_impression": rpi,
                "revenue_per_1000_impressions": round(
                    rpi * 1000, 2
                ),  # CPM equivalent
            }

        # Normalized no-data contract (Phase 4): a window with no
        # activated-video impressions is an explicit error, never
        # summary=None under status="success" (the ambiguity behind the
        # Phase-2 visualization crash).
        if not summary or not daily_metrics:
            return {
                "status": "error",
                "message": (
                    f"No metrics data available for campaign {campaign_id}. "
                    "Metrics only exist for activated videos — use the Review "
                    "Agent to activate videos first."
                ),
            }

        # Get count of activated videos
        cursor.execute(
            """
            SELECT COUNT(*) as count FROM campaign_videos
            WHERE campaign_id = ? AND status = 'activated'
        """,
            (campaign_id,),
        )
        video_count = cursor.fetchone()["count"]

        print(
            f"[DEBUG get_campaign_metrics] Found {len(daily_metrics)} daily records from {video_count} activated videos"
        )
        print(f"[DEBUG get_campaign_metrics] Summary: {summary}")
        return {
            "status": "success",
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "status": campaign["status"],
            },
            "activated_videos": video_count,
            "period": f"last_{days}_days",
            "summary": summary,
            "daily_metrics": daily_metrics,
        }


def get_top_performing_ads(
    metric: str = "revenue_per_impression",
    limit: int = 5,
    campaign_id: int | None = None,
    days: int | None = None,
) -> dict:
    """Get top performing video ads across all campaigns.

    Identifies the best ads and their key characteristics for insights.
    Uses NEW schema: video_metrics + campaign_videos + products (HITL workflow).
    Only includes activated videos. By default the ranking is global (all
    campaigns, all time); the optional filters narrow it when provided.

    Args:
        metric: Metric to rank by - one of: revenue_per_impression, impressions, dwell_time, circulation
        limit: Number of top ads to return
        campaign_id: Optional - restrict ranking to one campaign
        days: Optional - restrict ranking to the last N days of metrics

    Returns:
        Dictionary with top ads and their characteristics
    """
    print(
        f"[DEBUG get_top_performing_ads] Starting with metric={metric}, limit={limit}, campaign_id={campaign_id}, days={days}"
    )
    valid_metrics = [
        "revenue_per_impression",
        "impressions",
        "dwell_time",
        "circulation",
    ]
    if metric not in valid_metrics:
        return {
            "status": "error",
            "message": f"Invalid metric. Must be one of: {', '.join(valid_metrics)}",
        }

    # SQL ratio-of-sums used for ORDER BY ranking only — every RPI value
    # RETURNED to the caller comes from compute_rpi() (see the loop below).
    metric_column_map = {
        "revenue_per_impression": "SUM(vm.revenue) / NULLIF(SUM(vm.impressions), 0)",
        "impressions": "SUM(vm.impressions)",
        "dwell_time": "AVG(vm.dwell_time_seconds)",
        "circulation": "SUM(vm.circulation)",
    }

    with get_db_cursor() as cursor:
        vm_join = "LEFT JOIN video_metrics vm ON cv.id = vm.video_id"
        params: list = []
        if days:
            vm_join += " AND vm.metric_date >= date('now', ?)"
            params.append(f"-{days} days")
        where_clause = "WHERE cv.status = 'activated'"
        if campaign_id:
            where_clause += " AND cv.campaign_id = ?"
            params.append(campaign_id)
        params.append(limit)

        query = f"""
            SELECT
                cv.id as video_id,
                c.id as campaign_id,
                c.name as campaign_name,
                c.category,
                c.city,
                c.state,
                cv.video_filename,
                cv.variation_name,
                cv.variation_params,
                p.name as product_name,
                p.category as product_category,
                p.color as product_color,
                p.style as product_style,
                {metric_column_map[metric]} as metric_value,
                SUM(vm.impressions) as total_impressions,
                AVG(vm.dwell_time_seconds) as avg_dwell_time,
                SUM(vm.circulation) as total_circulation,
                SUM(vm.revenue) as total_revenue
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
            {vm_join}
            {where_clause}
            GROUP BY cv.id
            HAVING metric_value IS NOT NULL AND total_revenue > 0
            ORDER BY metric_value DESC
            LIMIT ?
        """  # noqa: S608
        cursor.execute(query, params)

        top_ads = []
        for row in cursor.fetchall():
            # Parse variation_params for characteristics
            variation_params = (
                json.loads(row["variation_params"])
                if row["variation_params"]
                else {}
            )
            total_impressions = (
                int(row["total_impressions"]) if row["total_impressions"] else 0
            )
            total_revenue = (
                round(row["total_revenue"], 2) if row["total_revenue"] else 0
            )
            rpi = compute_rpi(total_revenue, total_impressions)

            top_ads.append(
                {
                    "rank": len(top_ads) + 1,
                    "video_id": row["video_id"],
                    "campaign": {
                        "id": row["campaign_id"],
                        "name": row["campaign_name"],
                        "category": row["category"],
                        "location": f"{row['city']}, {row['state']}",
                    },
                    "product": {
                        "name": row["product_name"],
                        "category": row["product_category"],
                        "color": row["product_color"],
                        "style": row["product_style"],
                    },
                    "metrics": {
                        f"{metric}": rpi
                        if metric == "revenue_per_impression"
                        else (
                            round(row["metric_value"], 4)
                            if row["metric_value"]
                            else 0
                        ),
                        "total_impressions": total_impressions,
                        "average_dwell_time": round(row["avg_dwell_time"], 1)
                        if row["avg_dwell_time"]
                        else 0,
                        "total_circulation": int(row["total_circulation"])
                        if row["total_circulation"]
                        else 0,
                        "total_revenue": total_revenue,
                        "revenue_per_impression": rpi,
                    },
                    "characteristics": {
                        "variation": row["variation_name"],
                        "model_ethnicity": variation_params.get(
                            "model_ethnicity", "Unknown"
                        ),
                        "setting": variation_params.get("setting", "Unknown"),
                        "mood": variation_params.get("mood", "Unknown"),
                        "lighting": variation_params.get("lighting", "Unknown"),
                        "time_of_day": variation_params.get(
                            "time_of_day", "Unknown"
                        ),
                    },
                    "video_filename": row["video_filename"],
                }
            )

        # Extract common characteristics from top performers
        common_traits = {}
        if top_ads:
            for trait in ["setting", "mood", "model_ethnicity"]:
                values = [
                    ad["characteristics"].get(trait, "")
                    for ad in top_ads
                    if ad["characteristics"].get(trait)
                    and ad["characteristics"].get(trait) != "Unknown"
                ]
                if values:
                    from collections import Counter

                    most_common = Counter(values).most_common(1)
                    if most_common:
                        common_traits[trait] = most_common[0][0]

        print(
            f"[DEBUG get_top_performing_ads] Found {len(top_ads)} top video ads"
        )
        print(f"[DEBUG get_top_performing_ads] Common traits: {common_traits}")
        return {
            "status": "success",
            "ranked_by": metric,
            "top_ads": top_ads,
            "insights": {
                "common_characteristics": common_traits,
                "recommendation": f"Top performers share these traits: {', '.join(f'{k}={v}' for k, v in common_traits.items())}"
                if common_traits
                else "Insufficient data for recommendations",
            },
            "note": "Rankings based on activated videos only. Use Review Agent to activate pending videos.",
        }


def get_campaign_insights(campaign_id: int, days: int = 30) -> dict:
    """Get AI-generated insights about campaign performance.

    Analyzes campaign data and identifies key patterns and recommendations.
    Uses NEW schema: video_metrics + campaign_videos (HITL workflow).
    Only includes activated videos.

    Args:
        campaign_id: The ID of the campaign
        days: Number of days of metrics to analyze (default: 30)

    Returns:
        Dictionary with performance insights and recommendations
    """
    print(
        f"[DEBUG get_campaign_insights] Starting for campaign_id={campaign_id}"
    )
    with get_db_cursor() as cursor:
        # Get campaign details
        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Check if there are activated videos
        cursor.execute(
            """
            SELECT COUNT(*) as count FROM campaign_videos
            WHERE campaign_id = ? AND status = 'activated'
        """,
            (campaign_id,),
        )
        activated_count = cursor.fetchone()["count"]

        if activated_count == 0:
            return {
                "status": "success",
                "campaign": {
                    "id": campaign["id"],
                    "name": campaign["name"],
                    "category": campaign["category"],
                    "status": campaign["status"],
                },
                "performance_trend": "no_data",
                "insights": [
                    "No activated videos yet. Use Review Agent to activate pending videos."
                ],
                "recommendations": [
                    "Generate videos using Media Agent",
                    "Review and activate videos using Review Agent",
                    "Metrics will appear after activation",
                ],
                "best_day": None,
                "worst_day": None,
                "note": "Metrics only available for activated videos.",
            }

        # Get performance trend (weekly aggregates) - using RPI as key metric
        cursor.execute(
            """
            SELECT
                strftime('%Y-W%W', vm.metric_date) as week,
                SUM(vm.impressions) as impressions,
                SUM(vm.revenue) as revenue,
                AVG(vm.dwell_time_seconds) as avg_dwell
            FROM video_metrics vm
            JOIN campaign_videos cv ON vm.video_id = cv.id
            WHERE cv.campaign_id = ?
              AND cv.status = 'activated'
              AND vm.metric_date >= date('now', ?)
            GROUP BY week
            ORDER BY week
        """,
            (campaign_id, f"-{days} days"),
        )

        weeks = cursor.fetchall()

        # Trend compares RPI (ratio of sums per half), not raw revenue —
        # revenue can rise while RPI falls if impressions rise faster.
        trend = "stable"
        if len(weeks) >= 2:
            half = len(weeks) // 2
            first_half_rpi = compute_rpi(
                sum(w["revenue"] or 0 for w in weeks[:half]),
                sum(w["impressions"] or 0 for w in weeks[:half]),
            )
            second_half_rpi = compute_rpi(
                sum(w["revenue"] or 0 for w in weeks[half:]),
                sum(w["impressions"] or 0 for w in weeks[half:]),
            )
            if first_half_rpi > 0 and second_half_rpi > first_half_rpi * 1.1:
                trend = "improving"
            elif first_half_rpi > 0 and second_half_rpi < first_half_rpi * 0.9:
                trend = "declining"

        # Best and worst performing days by RPI — a "day" is the aggregate
        # across ALL activated videos that day (GROUP BY date), not one
        # video_metrics row. RPI per day via compute_rpi (ratio of sums).
        cursor.execute(
            """
            SELECT
                vm.metric_date as date,
                SUM(vm.revenue) as revenue,
                SUM(vm.impressions) as impressions,
                AVG(vm.dwell_time_seconds) as avg_dwell
            FROM video_metrics vm
            JOIN campaign_videos cv ON vm.video_id = cv.id
            WHERE cv.campaign_id = ?
              AND cv.status = 'activated'
              AND vm.metric_date >= date('now', ?)
            GROUP BY vm.metric_date
        """,
            (campaign_id, f"-{days} days"),
        )

        day_aggregates = [
            {
                "date": r["date"],
                "revenue_per_impression": compute_rpi(
                    r["revenue"], r["impressions"]
                ),
                "impressions": int(r["impressions"]) if r["impressions"] else 0,
                "dwell_time": round(r["avg_dwell"], 1) if r["avg_dwell"] else 0,
            }
            for r in cursor.fetchall()
        ]
        best_day = max(
            day_aggregates,
            key=lambda d: d["revenue_per_impression"],
            default=None,
        )
        days_with_impressions = [
            d for d in day_aggregates if d["impressions"] > 0
        ]
        worst_day = min(
            days_with_impressions,
            key=lambda d: d["revenue_per_impression"],
            default=None,
        )

        # Get video performance comparison
        cursor.execute(
            """
            SELECT
                cv.id,
                cv.variation_name,
                cv.variation_params,
                SUM(vm.revenue) as total_revenue,
                SUM(vm.impressions) as total_impressions,
                AVG(vm.dwell_time_seconds) as avg_dwell
            FROM campaign_videos cv
            LEFT JOIN video_metrics vm ON cv.id = vm.video_id
                AND vm.metric_date >= date('now', ?)
            WHERE cv.campaign_id = ?
              AND cv.status = 'activated'
            GROUP BY cv.id
            ORDER BY total_revenue DESC
        """,
            (f"-{days} days", campaign_id),
        )

        video_performances = cursor.fetchall()

        # Generate insights
        insights = []

        if trend == "improving":
            insights.append(
                "Campaign RPI is trending upward - consider increasing budget"
            )
        elif trend == "declining":
            insights.append(
                "Campaign RPI is declining - review creative and placement"
            )
        else:
            insights.append("Campaign performance is stable")

        if best_day:
            insights.append(
                f"Best performing day: {best_day['date']} "
                f"(RPI: ${best_day['revenue_per_impression']:.4f}, "
                f"Dwell: {best_day['dwell_time']}s)"
            )

        if video_performances:
            best_video = video_performances[0]
            if (
                best_video["total_impressions"]
                and best_video["total_impressions"] > 0
            ):
                video_rpi = compute_rpi(
                    best_video["total_revenue"], best_video["total_impressions"]
                )
                variation = best_video["variation_name"] or "default"
                avg_dwell = (
                    round(best_video["avg_dwell"], 1)
                    if best_video["avg_dwell"]
                    else 0
                )
                insights.append(
                    f"Top video (RPI: ${video_rpi:.4f}): {variation} variation, avg dwell {avg_dwell}s"
                )

                # Extract characteristics from variation_params
                if best_video["variation_params"]:
                    params = json.loads(best_video["variation_params"])
                    traits = []
                    if params.get("setting"):
                        traits.append(f"setting={params['setting']}")
                    if params.get("mood"):
                        traits.append(f"mood={params['mood']}")
                    if traits:
                        insights.append(
                            f"Winning characteristics: {', '.join(traits)}"
                        )

        print(
            f"[DEBUG get_campaign_insights] Trend: {trend}, Insights: {len(insights)} items"
        )
        return {
            "status": "success",
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "category": campaign["category"],
                "status": campaign["status"],
            },
            "activated_videos": activated_count,
            "period": f"last_{days}_days",
            "performance_trend": trend,
            "insights": insights,
            "recommendations": [
                "Consider generating variations of top-performing videos",
                "Test different settings and moods based on successful patterns",
                "Review dwell time patterns by time of week",
            ]
            if trend != "improving"
            else [
                "Continue current creative strategy",
                "Consider expanding to new locations",
                "Generate similar creatives for other campaigns",
            ],
            "best_day": best_day,
            "worst_day": worst_day,
        }


def compare_campaigns(campaign_ids: list[int]) -> dict:
    """Compare performance metrics across multiple campaigns.

    Compares in-store retail media metrics including impressions, dwell time,
    circulation, and revenue per impression (RPI).

    Uses NEW schema: video_metrics + campaign_videos (HITL workflow).
    Only includes activated videos.

    Args:
        campaign_ids: List of campaign IDs to compare

    Returns:
        Dictionary with comparative metrics and rankings
    """
    print(
        f"[DEBUG compare_campaigns] Starting with campaign_ids={campaign_ids}"
    )
    if not campaign_ids or len(campaign_ids) < 2:
        return {
            "status": "error",
            "message": "Please provide at least 2 campaign IDs to compare",
        }

    with get_db_cursor() as cursor:
        comparisons = []

        for cid in campaign_ids:
            cursor.execute(
                """
                SELECT
                    c.id,
                    c.name,
                    c.category,
                    c.city,
                    c.state,
                    c.status,
                    COUNT(DISTINCT cv.id) as video_count,
                    COUNT(DISTINCT CASE WHEN cv.status = 'activated' THEN cv.id END) as activated_count,
                    SUM(vm.impressions) as total_impressions,
                    AVG(vm.dwell_time_seconds) as avg_dwell_time,
                    SUM(vm.circulation) as total_circulation,
                    SUM(vm.revenue) as total_revenue
                FROM campaigns c
                LEFT JOIN campaign_videos cv ON c.id = cv.campaign_id
                LEFT JOIN video_metrics vm ON cv.id = vm.video_id AND cv.status = 'activated'
                WHERE c.id = ?
                GROUP BY c.id
            """,
                (cid,),
            )

            row = cursor.fetchone()
            if row:
                total_impressions = (
                    int(row["total_impressions"])
                    if row["total_impressions"]
                    else 0
                )
                total_revenue = (
                    round(row["total_revenue"], 2)
                    if row["total_revenue"]
                    else 0
                )
                rpi = compute_rpi(total_revenue, total_impressions)

                comparisons.append(
                    {
                        "campaign_id": row["id"],
                        "name": row["name"],
                        "category": row["category"],
                        "location": f"{row['city']}, {row['state']}",
                        "status": row["status"],
                        "video_count": row["video_count"] or 0,
                        "activated_videos": row["activated_count"] or 0,
                        "metrics": {
                            "total_impressions": total_impressions,
                            "average_dwell_time": round(
                                row["avg_dwell_time"], 1
                            )
                            if row["avg_dwell_time"]
                            else 0,
                            "total_circulation": int(row["total_circulation"])
                            if row["total_circulation"]
                            else 0,
                            "total_revenue": total_revenue,
                            "revenue_per_impression": rpi,
                            "revenue_per_1000_impressions": round(
                                rpi * 1000, 2
                            ),
                        },
                    }
                )

        if not comparisons:
            return {
                "status": "error",
                "message": "No valid campaigns found for the provided IDs",
            }

        # Rank by RPI (the primary KPI for retail media)
        ranked_by_rpi = sorted(
            comparisons,
            key=lambda x: x["metrics"]["revenue_per_impression"],
            reverse=True,
        )
        for i, c in enumerate(ranked_by_rpi):
            c["rpi_rank"] = i + 1

        # Rank by dwell time (engagement indicator)
        ranked_by_dwell = sorted(
            comparisons,
            key=lambda x: x["metrics"]["average_dwell_time"],
            reverse=True,
        )
        for i, c in enumerate(ranked_by_dwell):
            c["dwell_time_rank"] = i + 1

        # Find best performer by RPI
        best = ranked_by_rpi[0]

        # Check if any campaigns have no metrics
        no_metrics = [
            c for c in comparisons if c["metrics"]["total_impressions"] == 0
        ]

        print(
            f"[DEBUG compare_campaigns] Compared {len(comparisons)} campaigns"
        )
        print(f"[DEBUG compare_campaigns] Best performer: {best['name']}")
        return {
            "status": "success",
            "campaigns_compared": len(comparisons),
            "comparisons": comparisons,
            "best_performer": {
                "by_rpi": best["name"],
                "campaign_id": best["campaign_id"],
                "revenue_per_impression": best["metrics"][
                    "revenue_per_impression"
                ],
                "total_revenue": best["metrics"]["total_revenue"],
                "activated_videos": best["activated_videos"],
            },
            "summary": f"Compared {len(comparisons)} campaigns. '{best['name']}' leads with RPI of ${best['metrics']['revenue_per_impression']:.4f} (${best['metrics']['total_revenue']:.2f} total revenue).",
            "note": f"{len(no_metrics)} campaign(s) have no metrics yet. Activate videos using Review Agent."
            if no_metrics
            else None,
        }


def compare_creatives_within_campaign(campaign_id: int) -> dict:
    """Compare all activated creatives (video variations) within ONE campaign.

    Creative-level counterpart to compare_campaigns() (which compares whole
    campaigns): returns, per activated video, all-time totals and RPI via
    compute_rpi() (ratio of sums — see docs/METRICS.md), plus variation
    metadata so the comparison is legible. All-time window by design: demo
    metrics live in a fixed anchor window that date('now') filters can
    silently drift past.

    Args:
        campaign_id: The campaign whose creatives to compare

    Returns:
        Dictionary with per-creative metrics ranked by RPI
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id, name FROM campaigns WHERE id = ?", (campaign_id,)
        )
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign {campaign_id} not found",
            }

        cursor.execute(
            """
            SELECT
                cv.id AS video_id,
                cv.video_filename,
                cv.variation_name,
                cv.variation_params,
                SUM(vm.revenue) AS total_revenue,
                SUM(vm.impressions) AS total_impressions,
                AVG(vm.dwell_time_seconds) AS avg_dwell
            FROM campaign_videos cv
            LEFT JOIN video_metrics vm ON cv.id = vm.video_id
            WHERE cv.campaign_id = ? AND cv.status = 'activated'
            GROUP BY cv.id
        """,
            (campaign_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        return {
            "status": "error",
            "message": (
                f"Campaign {campaign_id} has no activated videos to compare. "
                "Use the Review Agent to activate videos first."
            ),
        }

    creatives = []
    for row in rows:
        characteristics = {}
        if row["variation_params"]:
            try:
                params = json.loads(row["variation_params"])
                characteristics = {
                    key: params.get(key)
                    for key in (
                        "setting",
                        "mood",
                        "model_ethnicity",
                        "lighting",
                        "time_of_day",
                    )
                    if params.get(key)
                }
            except json.JSONDecodeError:
                pass
        total_revenue = round(row["total_revenue"] or 0.0, 2)
        total_impressions = int(row["total_impressions"] or 0)
        creatives.append(
            {
                "video_id": row["video_id"],
                "video_filename": row["video_filename"],
                "variation_name": row["variation_name"] or "default",
                "characteristics": characteristics,
                "metrics": {
                    "total_impressions": total_impressions,
                    "total_revenue": total_revenue,
                    "average_dwell_time": round(row["avg_dwell"], 1)
                    if row["avg_dwell"]
                    else 0.0,
                    "revenue_per_impression": compute_rpi(
                        total_revenue, total_impressions
                    ),
                },
            }
        )

    creatives.sort(
        key=lambda c: c["metrics"]["revenue_per_impression"], reverse=True
    )
    for rank, creative in enumerate(creatives, start=1):
        creative["rpi_rank"] = rank

    best = creatives[0]
    return {
        "status": "success",
        "campaign_id": campaign_id,
        "campaign_name": campaign["name"],
        "creatives_compared": len(creatives),
        "creatives": creatives,
        "best_performer": {
            "video_id": best["video_id"],
            "variation_name": best["variation_name"],
            "revenue_per_impression": best["metrics"]["revenue_per_impression"],
        },
        "note": "RPI per creative = compute_rpi() over all-time sums (ratio of sums, never sum of ratios).",
    }


async def generate_creative_comparison_chart(
    campaign_id: int, tool_context: ToolContext = None
) -> dict:
    """Render a deterministic RPI-per-creative bar chart for one campaign.

    Unlike generate_metrics_visualization (AI-image-drawn), this renders with
    matplotlib, so bar heights are exactly the numbers returned — the chart
    and the structured data come from the same compare_creatives_within_campaign()
    call. Saved as a PNG artifact for the web UI; also returns the full
    comparison payload.

    Args:
        campaign_id: The campaign whose activated creatives to chart
        tool_context: ADK tool context for artifact saving

    Returns:
        Dictionary with chart artifact metadata and the comparison data
    """
    comparison = compare_creatives_within_campaign(campaign_id)
    if comparison["status"] != "success":
        return comparison

    creatives = comparison["creatives"]
    labels = [c["variation_name"] for c in creatives]
    rpi_values = [c["metrics"]["revenue_per_impression"] for c in creatives]

    # 16:9 to match the repo's chart convention (CHART_TEMPLATES enforce it)
    fig = Figure(figsize=(12.8, 7.2), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    bars = ax.bar(labels, rpi_values, color="#4C7DE0")
    ax.set_title(f"RPI by creative — {comparison['campaign_name']}")
    ax.set_ylabel("Revenue per impression ($)")
    ax.bar_label(bars, fmt="$%.4f")
    ax.tick_params(axis="x", labelrotation=15)
    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format="png")
    image_bytes = buffer.getvalue()

    timestamp = int(time.time())
    filename = f"creative_comparison_{campaign_id}_{timestamp}.png"
    if tool_context:
        image_artifact = types.Part.from_bytes(
            data=image_bytes, mime_type="image/png"
        )
        version = await tool_context.save_artifact(
            filename=filename, artifact=image_artifact
        )
        artifact_saved = True
    else:
        artifact_saved = False
        version = None

    return {
        "status": "success",
        "message": f"Creative comparison chart generated for campaign {campaign_id}",
        "chart": {
            "filename": filename,
            "artifact_saved": artifact_saved,
            "artifact_version": version,
            "chart_data": {"labels": labels, "rpi_values": rpi_values},
        },
        "comparison": comparison,
    }


def _aggregate_week(week_slice: list, metric: str) -> float:
    """Aggregate one week of enriched daily rows using the metric's rule.

    Ratio metrics recompute ratio-of-sums via compute_rpi; dwell time is an
    impressions-weighted average; additive metrics (impressions,
    circulation) sum. This is the per-metric aggregation rule Phase 4
    centralized — the old code summed daily values for every metric, which
    is meaningless for ratios (see docs/METRICS.md).
    """
    if metric == "revenue_per_impression":
        return compute_rpi(
            sum(d["revenue"] for d in week_slice),
            sum(d["impressions"] for d in week_slice),
        )
    if metric == "dwell_time":
        return round(
            compute_weighted_average(week_slice, "dwell_time", "impressions"), 1
        )
    return sum(d["value"] for d in week_slice)


async def generate_metrics_visualization(
    campaign_id: int,
    chart_type: str = "trendline",
    metric: str = "revenue_per_impression",
    days: int = 30,
    tool_context: ToolContext = None,
) -> dict:
    """Generate a visual chart/infographic from campaign metrics using Gemini 3 Pro Image.

    Creates professional data visualizations as images using AI image generation.
    The generated chart is saved as an ADK artifact for viewing in the web UI.

    Args:
        campaign_id: The campaign to visualize metrics for
        chart_type: Type of visualization - one of: trendline, bar_chart, comparison, infographic
        metric: Which metric to visualize - one of: revenue_per_impression, impressions, dwell_time, circulation
        days: Number of days of data to include (default: 30)
        tool_context: ADK ToolContext for artifact storage

    Returns:
        Dictionary with visualization details and artifact info
    """
    print(
        f"[DEBUG generate_metrics_visualization] Starting for campaign_id={campaign_id}"
    )
    print(
        f"[DEBUG generate_metrics_visualization] chart_type={chart_type}, metric={metric}, days={days}"
    )

    valid_chart_types = ["trendline", "bar_chart", "comparison", "infographic"]
    valid_metrics = [
        "revenue_per_impression",
        "impressions",
        "dwell_time",
        "circulation",
    ]

    if chart_type not in valid_chart_types:
        return {
            "status": "error",
            "message": f"Invalid chart_type. Must be one of: {', '.join(valid_chart_types)}",
        }

    if metric not in valid_metrics:
        return {
            "status": "error",
            "message": f"Invalid metric. Must be one of: {', '.join(valid_metrics)}",
        }

    # Get campaign metrics data
    print("[DEBUG VIZ] Step 1: Fetching metrics from database...")
    metrics_result = get_campaign_metrics(campaign_id, days)
    if metrics_result["status"] == "error":
        return metrics_result

    campaign_name = metrics_result["campaign"]["name"]
    summary = metrics_result["summary"]
    daily_metrics = metrics_result["daily_metrics"]

    print("[DEBUG VIZ] Step 2: Data received from DB:")
    print(f"[DEBUG VIZ]   - Campaign: {campaign_name}")
    print(f"[DEBUG VIZ]   - Total daily records: {len(daily_metrics)}")
    print(
        f"[DEBUG VIZ]   - Summary totals: impressions={summary['total_impressions']:,}, revenue=${summary['total_revenue']:,.2f}"
    )

    # Show first 3 and last 3 daily records as sample
    print("[DEBUG VIZ]   - Sample daily data (first 3 records):")
    for i, day in enumerate(daily_metrics[:3]):
        print(
            f"[DEBUG VIZ]     [{i}] date={day['date']}, {metric}={day.get(metric, 'N/A')}"
        )
    if len(daily_metrics) > 6:
        print(
            f"[DEBUG VIZ]     ... ({len(daily_metrics) - 6} more records) ..."
        )
    if len(daily_metrics) > 3:
        print("[DEBUG VIZ]   - Sample daily data (last 3 records):")
        for i, day in enumerate(daily_metrics[-3:]):
            print(
                f"[DEBUG VIZ]     [{len(daily_metrics) - 3 + i}] date={day['date']}, {metric}={day.get(metric, 'N/A')}"
            )

    # Extract data points for the visualization
    print(
        f"[DEBUG VIZ] Step 3: Extracting '{metric}' values from daily_metrics..."
    )
    data_points = []
    for day in daily_metrics[: min(days, len(daily_metrics))]:
        data_points.append(
            {
                "date": day["date"],
                "value": day.get(metric, 0),
                "revenue": day.get("revenue", 0),
                "impressions": day.get("impressions", 0),
                "dwell_time": day.get("dwell_time", 0),
            }
        )

    # Reverse to show oldest to newest
    data_points = list(reversed(data_points))

    print(
        f"[DEBUG VIZ]   - Extracted {len(data_points)} data points (oldest to newest)"
    )
    print(
        f"[DEBUG VIZ]   - First point: date={data_points[0]['date']}, value={data_points[0]['value']}"
    )
    print(
        f"[DEBUG VIZ]   - Last point: date={data_points[-1]['date']}, value={data_points[-1]['value']}"
    )

    # Calculate statistics for the prompt
    values = [d["value"] for d in data_points]
    min_val = min(values) if values else 0
    max_val = max(values) if values else 0
    avg_val = sum(values) / len(values) if values else 0
    total_val = sum(values)

    print("[DEBUG VIZ] Step 4: Calculated statistics:")
    print(f"[DEBUG VIZ]   - Min: {min_val}")
    print(f"[DEBUG VIZ]   - Max: {max_val}")
    print(f"[DEBUG VIZ]   - Avg: {avg_val:.2f}")
    print(f"[DEBUG VIZ]   - Sum: {total_val}")

    # Determine trend
    if len(values) >= 2:
        first_half = sum(values[: len(values) // 2]) / (len(values) // 2)
        second_half = sum(values[len(values) // 2 :]) / (
            len(values) - len(values) // 2
        )
        print(f"[DEBUG VIZ]   - First half avg: {first_half:.2f}")
        print(f"[DEBUG VIZ]   - Second half avg: {second_half:.2f}")
        if second_half > first_half * 1.05:
            trend = "upward trending"
        elif second_half < first_half * 0.95:
            trend = "downward trending"
        else:
            trend = "stable"
    else:
        trend = "stable"

    print(f"[DEBUG VIZ]   - Trend: {trend}")

    # Format metric name for display
    metric_display = metric.replace("_", " ").title()
    if metric == "revenue_per_impression":
        metric_display = "Revenue Per Impression (RPI)"
        value_format = f"${min_val:.4f} to ${max_val:.4f}"
    elif metric == "dwell_time":
        metric_display = "Dwell Time (seconds)"
        value_format = f"{min_val:.1f}s to {max_val:.1f}s"
    elif metric == "circulation":
        metric_display = "Circulation (foot traffic)"
        value_format = f"{int(min_val):,} to {int(max_val):,}"
    else:
        value_format = f"{int(min_val):,} to {int(max_val):,}"

    print(
        f"[DEBUG VIZ]   - Display format: {metric_display}, range: {value_format}"
    )

    # Build the visualization prompt using structured templates
    print(
        f"[DEBUG VIZ] Step 5: Building prompt from CHART_TEMPLATES for chart_type='{chart_type}'..."
    )

    # Prepare common template variables
    template_vars = {
        "campaign_name": campaign_name,
        "metric_display": metric_display,
        "days": days,
        "value_format": value_format,
        "trend": trend,
        "num_points": len(data_points),
        "rpi": summary["revenue_per_impression"],
        "impressions": summary["total_impressions"],
        "dwell_time": summary["average_dwell_time"],
        "circulation": summary["total_circulation"],
    }

    if chart_type == "trendline":
        # Format data points for template
        data_points_formatted = "\n".join(
            [
                f"   - {dp['date']}: {dp['value']:.4f}"
                if metric == "revenue_per_impression"
                else f"   - {dp['date']}: {dp['value']:.1f}"
                if metric == "dwell_time"
                else f"   - {dp['date']}: {int(dp['value']):,}"
                for dp in data_points
            ]
        )

        # Format statistics based on metric type
        if metric == "revenue_per_impression":
            avg_formatted = f"${avg_val:.4f}"
            max_formatted = f"${max_val:.4f}"
            min_formatted = f"${min_val:.4f}"
        elif metric == "dwell_time":
            avg_formatted = f"{avg_val:.1f}s"
            max_formatted = f"{max_val:.1f}s"
            min_formatted = f"{min_val:.1f}s"
        else:
            avg_formatted = f"{int(avg_val):,}"
            max_formatted = f"{int(max_val):,}"
            min_formatted = f"{int(min_val):,}"

        template_vars.update(
            {
                "start_date": data_points[0]["date"],
                "end_date": data_points[-1]["date"],
                "data_points_formatted": data_points_formatted,
                "avg_val": avg_formatted,
                "max_val": max_formatted,
                "min_val": min_formatted,
            }
        )

        visualization_prompt = CHART_TEMPLATES["trendline"].format(
            **template_vars
        )

    elif chart_type == "bar_chart":
        # Get weekly aggregates for bar chart
        print("[DEBUG VIZ]   - Aggregating data into weekly buckets...")
        weekly_data = []
        week_size = 7
        for i in range(0, len(data_points), week_size):
            week_slice = data_points[i : i + week_size]
            if week_slice:
                week_value = _aggregate_week(week_slice, metric)
                weekly_data.append(
                    {
                        "week": f"Week {len(weekly_data) + 1}",
                        "value": week_value,
                    }
                )
                print(
                    f"[DEBUG VIZ]     Week {len(weekly_data)}: {len(week_slice)} days, value={week_value:.4f}"
                )

        # Format weekly data for template
        if metric == "revenue_per_impression":
            weekly_data_formatted = "\n".join(
                [f"   - {w['week']}: ${w['value']:.4f}" for w in weekly_data]
            )
        elif metric == "dwell_time":
            weekly_data_formatted = "\n".join(
                [f"   - {w['week']}: {w['value']:.1f}s" for w in weekly_data]
            )
        else:
            weekly_data_formatted = "\n".join(
                [f"   - {w['week']}: {int(w['value']):,}" for w in weekly_data]
            )

        template_vars.update(
            {
                "weekly_data_formatted": weekly_data_formatted,
                "num_bars": len(weekly_data),
            }
        )

        visualization_prompt = CHART_TEMPLATES["bar_chart"].format(
            **template_vars
        )

    elif chart_type == "comparison":
        # Log the exact values being sent
        print("[DEBUG VIZ]   - Comparison chart using summary metrics:")
        print(f"[DEBUG VIZ]     RPI: ${summary['revenue_per_impression']:.4f}")
        print(f"[DEBUG VIZ]     Impressions: {summary['total_impressions']:,}")
        print(
            f"[DEBUG VIZ]     Dwell Time: {summary['average_dwell_time']:.1f}s"
        )
        print(f"[DEBUG VIZ]     Circulation: {summary['total_circulation']:,}")

        visualization_prompt = CHART_TEMPLATES["comparison"].format(
            **template_vars
        )

    else:  # infographic
        # Format primary value based on metric
        if metric == "revenue_per_impression":
            primary_value = f"${avg_val:.4f}"
        elif metric == "dwell_time":
            primary_value = f"{avg_val:.1f}s"
        else:
            primary_value = f"{int(avg_val):,}"

        print("[DEBUG VIZ]   - Infographic using all data:")
        print(f"[DEBUG VIZ]     Primary metric: {metric_display}")
        print(f"[DEBUG VIZ]     Primary value: {primary_value}")
        print(f"[DEBUG VIZ]     RPI: ${summary['revenue_per_impression']:.4f}")

        template_vars.update(
            {
                "primary_value": primary_value,
            }
        )

        visualization_prompt = CHART_TEMPLATES["infographic"].format(
            **template_vars
        )

    print(
        "[DEBUG VIZ] Step 6: Complete prompt being sent to Gemini 3 Pro Image:"
    )
    print(f"[DEBUG VIZ] {'=' * 60}")
    print(visualization_prompt)
    print(f"[DEBUG VIZ] {'=' * 60}")
    print(f"[DEBUG VIZ] Prompt length: {len(visualization_prompt)} characters")

    try:
        print("[DEBUG VIZ] Step 7: Calling Gemini 3 Pro Image API...")
        client = genai.Client()

        # Generate visualization using Gemini 3 Pro Image
        response = client.models.generate_content(
            model=IMAGE_GENERATION,
            contents=[visualization_prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio="16:9",  # Wide format for charts
                ),
            ),
        )
        print(
            f"[DEBUG VIZ]   - Response received, parts count: {len(response.parts) if response.parts else 0}"
        )

        # Extract image from response
        generated_image = None
        for i, part in enumerate(response.parts):
            has_inline = (
                hasattr(part, "inline_data") and part.inline_data is not None
            )
            print(f"[DEBUG VIZ]   - Part {i}: has inline_data={has_inline}")
            if part.inline_data:
                generated_image = part
                print(
                    f"[DEBUG VIZ]   - Image found in part {i}, size: {len(part.inline_data.data)} bytes"
                )
                break

        if generated_image is None:
            print("[DEBUG VIZ]   - ERROR: No image found in response")
            return {
                "status": "error",
                "message": "Failed to generate visualization. Try a different chart type or metric.",
            }

        # Save as ADK artifact (not locally)
        timestamp = int(time.time())
        filename = f"chart_{campaign_id}_{chart_type}_{metric}_{timestamp}.png"

        print("[DEBUG VIZ] Step 8: Saving artifact...")
        if tool_context:
            print(f"[DEBUG VIZ]   - Filename: {filename}")
            # Get the image bytes from inline_data
            image_bytes = generated_image.inline_data.data
            image_artifact = types.Part.from_bytes(
                data=image_bytes, mime_type="image/png"
            )
            version = await tool_context.save_artifact(
                filename=filename, artifact=image_artifact
            )
            print(
                f"[DEBUG VIZ]   - Artifact saved successfully, version: {version}"
            )
            artifact_saved = True
        else:
            print(
                "[DEBUG VIZ]   - WARNING: No tool_context, artifact not saved"
            )
            artifact_saved = False
            version = None

        print("[DEBUG VIZ] Step 9: SUCCESS - Visualization complete!")
        print(f"[DEBUG VIZ]   - Data points used: {len(data_points)}")
        print(
            f"[DEBUG VIZ]   - Statistics: min={min_val}, max={max_val}, avg={avg_val:.2f}"
        )
        print(f"[DEBUG VIZ]   - Trend: {trend}")

        return {
            "status": "success",
            "message": f"Generated {chart_type} visualization for {campaign_name}",
            "visualization": {
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "chart_type": chart_type,
                "metric": metric,
                "days": days,
                "filename": filename,
                "artifact_saved": artifact_saved,
                "artifact_version": version,
                "data_summary": {
                    "data_points": len(data_points),
                    "min": min_val,
                    "max": max_val,
                    "average": round(avg_val, 2),
                    "trend": trend,
                },
            },
        }

    except Exception as e:
        import traceback

        print(f"[DEBUG VIZ] EXCEPTION: {e!s}")
        print(f"[DEBUG VIZ] Traceback: {traceback.format_exc()}")
        return {
            "status": "error",
            "message": f"Failed to generate visualization: {e!s}",
        }
