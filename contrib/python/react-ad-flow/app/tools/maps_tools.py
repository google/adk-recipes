# Copyright 2026 Google LLC
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

"""Google Maps tools for campaign location visualization."""

import time
from typing import Any

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

from ..config import GOOGLE_MAPS_API_KEY, IMAGE_GENERATION
from ..database.db import get_db_cursor
from .metrics_shared import compute_rpi, compute_weighted_average


def get_campaign_locations() -> dict:
    """Get geographic locations of all campaigns for map display.

    Geocodes campaign city/state to coordinates and includes metrics summary
    for map visualization.

    Returns:
        Dictionary with campaign locations and coordinates
    """
    try:
        import googlemaps
    except ImportError:
        return {
            "status": "error",
            "message": "googlemaps package not installed. Run: uv add googlemaps",
        }

    api_key = GOOGLE_MAPS_API_KEY
    if not api_key:
        return {
            "status": "error",
            "message": "GOOGLE_MAPS_API_KEY environment variable not set",
        }

    gmaps = googlemaps.Client(key=api_key)

    with get_db_cursor() as cursor:
        # Current schema: campaign_videos + video_metrics (HITL workflow).
        # ad_count = activated videos; metrics only exist for activated videos.
        cursor.execute("""
            SELECT
                c.id,
                c.name,
                c.category,
                c.city,
                c.state,
                c.status,
                COUNT(DISTINCT CASE WHEN cv.status = 'activated' THEN cv.id END) as ad_count,
                SUM(vm.revenue) as total_revenue,
                SUM(vm.impressions) as total_impressions
            FROM campaigns c
            LEFT JOIN campaign_videos cv ON c.id = cv.campaign_id
            LEFT JOIN video_metrics vm ON cv.id = vm.video_id AND cv.status = 'activated'
            GROUP BY c.id
        """)

        campaigns = cursor.fetchall()

    locations = []
    geocode_cache: dict[str, dict[str, float] | None] = {}

    for campaign in campaigns:
        location_key = f"{campaign['city']}, {campaign['state']}"

        # Use cache to avoid duplicate geocoding
        if location_key not in geocode_cache:
            try:
                geocode_result = gmaps.geocode(location_key)
                if geocode_result:
                    lat = geocode_result[0]["geometry"]["location"]["lat"]
                    lng = geocode_result[0]["geometry"]["location"]["lng"]
                    geocode_cache[location_key] = {"lat": lat, "lng": lng}
                else:
                    geocode_cache[location_key] = None
            except Exception:
                geocode_cache[location_key] = None

        coords = geocode_cache.get(location_key)

        locations.append(
            {
                "campaign_id": campaign["id"],
                "name": campaign["name"],
                "category": campaign["category"],
                "status": campaign["status"],
                "location": {
                    "city": campaign["city"],
                    "state": campaign["state"],
                    "coordinates": coords,
                },
                "metrics": {
                    "ad_count": campaign["ad_count"] or 0,
                    "total_revenue": round(campaign["total_revenue"], 2)
                    if campaign["total_revenue"]
                    else 0,
                    "total_impressions": int(campaign["total_impressions"])
                    if campaign["total_impressions"]
                    else 0,
                },
            }
        )

    # Generate Google Maps URL for visualization
    if locations:
        # Create a simple map URL centered on US
        map_center = "39.8283,-98.5795"  # Center of US
        map_url = f"https://www.google.com/maps/dir/?api=1&origin={map_center}&destination={map_center}"
    else:
        map_url = None

    return {
        "status": "success",
        "campaign_count": len(locations),
        "locations": locations,
        "map_visualization": {
            "center": {"lat": 39.8283, "lng": -98.5795},
            "zoom": 4,
            "map_url": map_url,
        },
    }


def search_nearby_stores(
    city: str,
    state: str,
    business_type: str = "retail store",
    radius_meters: int = 5000,
) -> dict:
    """Search for retail stores near a campaign location.

    Useful for competitive analysis and location strategy.

    Args:
        city: City name
        state: State abbreviation
        business_type: Type of business to search (default: "retail store")
        radius_meters: Search radius in meters (default: 5000)

    Returns:
        Dictionary with nearby places
    """
    try:
        import googlemaps
    except ImportError:
        return {
            "status": "error",
            "message": "googlemaps package not installed. Run: uv add googlemaps",
        }

    api_key = GOOGLE_MAPS_API_KEY
    if not api_key:
        return {
            "status": "error",
            "message": "GOOGLE_MAPS_API_KEY environment variable not set",
        }

    gmaps = googlemaps.Client(key=api_key)

    try:
        # Geocode the location first
        location_str = f"{city}, {state}"
        geocode_result = gmaps.geocode(location_str)

        if not geocode_result:
            return {
                "status": "error",
                "message": f"Could not geocode location: {location_str}",
            }

        lat = geocode_result[0]["geometry"]["location"]["lat"]
        lng = geocode_result[0]["geometry"]["location"]["lng"]

        # Search for nearby places
        places_result = gmaps.places_nearby(
            location=(lat, lng), radius=radius_meters, keyword=business_type
        )

        places = []
        for place in places_result.get("results", [])[
            :10
        ]:  # Limit to 10 results
            places.append(
                {
                    "name": place.get("name"),
                    "address": place.get("vicinity"),
                    "rating": place.get("rating"),
                    "user_ratings_total": place.get("user_ratings_total"),
                    "place_id": place.get("place_id"),
                    "types": place.get("types", []),
                    "location": {
                        "lat": place.get("geometry", {})
                        .get("location", {})
                        .get("lat"),
                        "lng": place.get("geometry", {})
                        .get("location", {})
                        .get("lng"),
                    },
                }
            )

        return {
            "status": "success",
            "search_location": location_str,
            "search_type": business_type,
            "radius_meters": radius_meters,
            "results_count": len(places),
            "places": places,
        }

    except Exception as e:
        return {"status": "error", "message": f"Places search failed: {e!s}"}


def get_location_demographics(city: str, state: str) -> dict:
    """Get demographic and market information for a location.

    Note: This provides simulated demographic data for demo purposes.
    In production, this would integrate with real demographic data sources.

    Args:
        city: City name
        state: State abbreviation

    Returns:
        Dictionary with location demographics and market data
    """
    # Simulated demographic data for demo purposes
    # In production, this would use real census/demographic APIs
    city_data: dict[str, dict[str, Any]] = {
        "Los Angeles, CA": {
            "population": 3900000,
            "median_age": 35,
            "median_income": 65000,
            "retail_market_index": 92,
            "style_preference": ["casual", "athleisure", "bohemian"],
        },
        "New York, NY": {
            "population": 8300000,
            "median_age": 36,
            "median_income": 72000,
            "retail_market_index": 98,
            "style_preference": ["formal", "contemporary", "luxury"],
        },
        "Chicago, IL": {
            "population": 2700000,
            "median_age": 34,
            "median_income": 58000,
            "retail_market_index": 78,
            "style_preference": ["professional", "classic", "urban"],
        },
        "Seattle, WA": {
            "population": 750000,
            "median_age": 36,
            "median_income": 85000,
            "retail_market_index": 72,
            "style_preference": ["casual", "outdoor", "sustainable"],
        },
    }

    location_key = f"{city}, {state}"
    data = city_data.get(location_key)

    if data:
        return {
            "status": "success",
            "location": location_key,
            "demographics": data,
            "market_insight": f"{city} has a retail market index of {data['retail_market_index']}/100, "
            f"with preferences for {', '.join(data['style_preference'])} styles.",
        }
    else:
        return {
            "status": "success",
            "location": location_key,
            "demographics": {
                "population": "Data not available",
                "retail_market_index": 50,
                "style_preference": ["general"],
            },
            "market_insight": f"Detailed demographic data not available for {location_key}. Using default market assumptions.",
        }


# Simulated coordinates for demo (avoid API calls for visualization)
CITY_COORDINATES = {
    "Los Angeles, CA": {"lat": 34.0522, "lng": -118.2437},
    "New York, NY": {"lat": 40.7128, "lng": -74.0060},
    "Chicago, IL": {"lat": 41.8781, "lng": -87.6298},
    "Seattle, WA": {"lat": 47.6062, "lng": -122.3321},
}

# Region mapping for analysis
REGION_MAPPING = {
    "CA": "West Coast",
    "WA": "West Coast",
    "OR": "West Coast",
    "NY": "East Coast",
    "NJ": "East Coast",
    "MA": "East Coast",
    "IL": "Midwest",
    "OH": "Midwest",
    "MI": "Midwest",
    "TX": "South",
    "FL": "South",
    "GA": "South",
}


# =============================================================================
# Google Maps URL Helper Functions
# =============================================================================


def get_google_maps_url(
    lat: float, lng: float, label: str | None = None, zoom: int = 15
) -> str:
    """Generate a direct Google Maps URL for a location.

    Opens the location in Google Maps when clicked (works on mobile and desktop).

    Args:
        lat: Latitude
        lng: Longitude
        label: Optional label for the place (used in search query)
        zoom: Zoom level (1-20, default 15)

    Returns:
        Direct Google Maps URL string
    """
    if label:
        # Use search query format for labeled locations
        import urllib.parse

        query = urllib.parse.quote(f"{label} @{lat},{lng}")
        return f"https://www.google.com/maps/search/?api=1&query={query}&zoom={zoom}"
    else:
        # Direct coordinate format
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}&zoom={zoom}"


def get_google_maps_place_url(place_id: str) -> str:
    """Generate Google Maps URL from a Place ID.

    Place IDs are returned by Google Places API and provide exact locations.

    Args:
        place_id: Google Places API place ID

    Returns:
        Google Maps URL for the place
    """
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def get_google_maps_directions_url(
    origin: tuple,
    destination: tuple,
    waypoints: list | None = None,
    mode: str = "driving",
) -> str:
    """Generate Google Maps directions URL.

    Args:
        origin: (lat, lng) tuple for start location
        destination: (lat, lng) tuple for end location
        waypoints: Optional list of (lat, lng) waypoint tuples
        mode: Travel mode (driving, walking, bicycling, transit)

    Returns:
        Google Maps directions URL
    """
    url = "https://www.google.com/maps/dir/?api=1"
    url += f"&origin={origin[0]},{origin[1]}"
    url += f"&destination={destination[0]},{destination[1]}"
    url += f"&travelmode={mode}"

    if waypoints:
        wp_str = "|".join([f"{wp[0]},{wp[1]}" for wp in waypoints])
        url += f"&waypoints={wp_str}"

    return url


# =============================================================================
# Rich Campaign Map Data
# =============================================================================


def get_campaign_map_data(
    campaign_id: int | None = None,
    include_videos: bool = True,
    include_products: bool = True,
    include_metrics: bool = True,
) -> dict:
    """Get rich campaign map data with Google Maps links.

    Returns comprehensive location data with:
    - Direct Google Maps URLs for each store
    - Product images (GCS public URLs)
    - Video thumbnails and video URLs
    - Performance metrics per location
    - Zone/area suggestions

    Args:
        campaign_id: Optional filter for specific campaign
        include_videos: Include video URLs and thumbnails
        include_products: Include product info and images
        include_metrics: Include performance metrics

    Returns:
        Dictionary with rich location data and links
    """
    from .. import storage

    with get_db_cursor() as cursor:
        # Build query based on filters
        query = """
            SELECT
                c.id as campaign_id,
                c.name as campaign_name,
                c.store_name,
                c.city,
                c.state,
                c.status,
                c.category,
                p.id as product_id,
                p.name as product_name,
                p.category as product_category,
                p.color as product_color,
                p.style as product_style,
                p.fabric as product_fabric,
                p.image_filename as product_image
            FROM campaigns c
            LEFT JOIN products p ON c.product_id = p.id
        """

        if campaign_id:
            query += " WHERE c.id = ?"
            cursor.execute(query, (campaign_id,))
        else:
            cursor.execute(query)

        campaigns = cursor.fetchall()

        if not campaigns:
            return {
                "status": "error",
                "message": "No campaigns found"
                + (f" for id {campaign_id}" if campaign_id else ""),
            }

        locations = []
        total_revenue = 0
        total_impressions = 0
        active_videos = 0

        for camp in campaigns:
            location_key = f"{camp['city']}, {camp['state']}"
            coords = CITY_COORDINATES.get(
                location_key, {"lat": 39.8, "lng": -98.5}
            )

            # Build location data
            loc_data = {
                "campaign_id": camp["campaign_id"],
                "campaign_name": camp["campaign_name"],
                "store_name": camp["store_name"],
                "city": camp["city"],
                "state": camp["state"],
                "status": camp["status"],
                "category": camp["category"],
                "coordinates": coords,
                "google_maps_url": get_google_maps_url(
                    coords["lat"], coords["lng"], label=camp["store_name"]
                ),
            }

            # Add product info
            if include_products and camp["product_id"]:
                product_image_url = None
                if camp["product_image"]:
                    product_image_url = storage.get_product_image_public_url(
                        camp["product_image"]
                    )

                loc_data["product"] = {
                    "id": camp["product_id"],
                    "name": camp["product_name"],
                    "category": camp["product_category"],
                    "color": camp["product_color"],
                    "style": camp["product_style"],
                    "fabric": camp["product_fabric"],
                    "image_url": product_image_url,
                }

            # Add videos
            if include_videos:
                cursor.execute(
                    """
                    SELECT id, video_filename, thumbnail_path, variation_name, status
                    FROM campaign_videos
                    WHERE campaign_id = ?
                    ORDER BY created_at DESC
                """,
                    (camp["campaign_id"],),
                )
                videos = cursor.fetchall()

                video_list = []
                for vid in videos:
                    video_url = None
                    video_exists = False
                    if vid["video_filename"]:
                        video_url = storage.get_video_public_url(
                            vid["video_filename"], check_exists=True
                        )
                        video_exists = video_url is not None
                    thumb_filename = (
                        vid["thumbnail_path"].split("/")[-1]
                        if vid["thumbnail_path"]
                        and "/" in vid["thumbnail_path"]
                        else vid["thumbnail_path"]
                    )
                    thumbnail_url = (
                        storage.get_thumbnail_public_url(thumb_filename)
                        if thumb_filename
                        else None
                    )

                    video_list.append(
                        {
                            "id": vid["id"],
                            "video_url": video_url,
                            "video_exists": video_exists,
                            "thumbnail_url": thumbnail_url,
                            "variation": vid["variation_name"],
                            "status": vid["status"],
                        }
                    )

                    if vid["status"] == "activated":
                        active_videos += 1

                loc_data["videos"] = video_list

            # Add metrics
            if include_metrics:
                cursor.execute(
                    """
                    SELECT
                        SUM(impressions) as total_impressions,
                        AVG(dwell_time_seconds) as avg_dwell,
                        SUM(circulation) as total_circulation,
                        SUM(revenue) as total_revenue
                    FROM video_metrics vm
                    JOIN campaign_videos cv ON vm.video_id = cv.id
                    WHERE cv.campaign_id = ?
                """,
                    (camp["campaign_id"],),
                )
                metrics = cursor.fetchone()

                if metrics and metrics["total_impressions"]:
                    camp_revenue = (
                        round(metrics["total_revenue"], 2)
                        if metrics["total_revenue"]
                        else 0
                    )
                    camp_impressions = int(metrics["total_impressions"])
                    rpi = compute_rpi(camp_revenue, camp_impressions)

                    loc_data["metrics"] = {
                        "total_revenue": camp_revenue,
                        "total_impressions": camp_impressions,
                        "avg_dwell_time": round(metrics["avg_dwell"], 1)
                        if metrics["avg_dwell"]
                        else 0,
                        "total_circulation": int(metrics["total_circulation"])
                        if metrics["total_circulation"]
                        else 0,
                        "rpi": rpi,
                    }

                    total_revenue += camp_revenue
                    total_impressions += camp_impressions
                else:
                    loc_data["metrics"] = None

            locations.append(loc_data)

        return {
            "status": "success",
            "location_count": len(locations),
            "locations": locations,
            "summary": {
                "total_campaigns": len(locations),
                "total_revenue": round(total_revenue, 2),
                "total_impressions": total_impressions,
                "active_videos": active_videos,
                "overall_rpi": compute_rpi(total_revenue, total_impressions),
            },
            "message": "Click google_maps_url links to open store locations in Google Maps",
        }


# =============================================================================
# Google Static Maps API
# =============================================================================


def generate_static_map(
    locations: list | None = None,
    map_type: str = "roadmap",
    size: str = "640x480",
    zoom: int | None = None,
    markers: bool = True,
    color_by: str = "status",
) -> dict:
    """Generate a Google Static Maps image with markers.

    Uses Google Static Maps API to create real map images with markers
    at campaign locations. Markers are color-coded based on the color_by parameter.

    Args:
        locations: List of location dicts with lat/lng. If None, uses all campaigns.
        map_type: Map type (roadmap, satellite, terrain, hybrid)
        size: Image size in pixels (e.g., "640x480", "800x600")
        zoom: Zoom level (1-20). If None, auto-fits all markers.
        markers: Whether to show markers
        color_by: How to color markers - "status" (active=green, draft=gray) or
                  "revenue" (high=green, medium=yellow, low=red)

    Returns:
        Dictionary with static map URL and marker data
    """
    if not GOOGLE_MAPS_API_KEY:
        return {
            "status": "error",
            "message": "GOOGLE_MAPS_API_KEY environment variable not set",
        }

    # Get campaign locations if not provided
    if locations is None:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT
                    c.id, c.name, c.city, c.state, c.status,
                    COALESCE(SUM(vm.revenue), 0) as total_revenue
                FROM campaigns c
                LEFT JOIN campaign_videos cv ON c.id = cv.campaign_id
                LEFT JOIN video_metrics vm ON cv.id = vm.video_id
                GROUP BY c.id
            """)
            campaigns = cursor.fetchall()

        locations = []
        for camp in campaigns:
            location_key = f"{camp['city']}, {camp['state']}"
            coords = CITY_COORDINATES.get(location_key)
            if coords:
                locations.append(
                    {
                        "id": camp["id"],
                        "name": camp["name"],
                        "lat": coords["lat"],
                        "lng": coords["lng"],
                        "status": camp["status"],
                        "revenue": camp["total_revenue"] or 0,
                    }
                )

    if not locations:
        return {
            "status": "error",
            "message": "No locations found to display on map",
        }

    # Build Static Maps URL
    base_url = "https://maps.googleapis.com/maps/api/staticmap?"
    params = [
        f"size={size}",
        f"maptype={map_type}",
        f"key={GOOGLE_MAPS_API_KEY}",
    ]

    if zoom:
        params.append(f"zoom={zoom}")
        # Use center of first location if zoom is specified
        params.append(f"center={locations[0]['lat']},{locations[0]['lng']}")

    # Add markers
    marker_data = []
    if markers:
        for i, loc in enumerate(locations):
            # Determine marker color
            if color_by == "status":
                color = "green" if loc.get("status") == "active" else "gray"
            else:  # revenue
                revenue = loc.get("revenue", 0)
                if revenue > 30000:
                    color = "green"
                elif revenue > 15000:
                    color = "yellow"
                else:
                    color = "red"

            label = str(i + 1)
            lat, lng = loc["lat"], loc["lng"]

            params.append(
                f"markers=color:{color}%7Clabel:{label}%7C{lat},{lng}"
            )

            marker_data.append(
                {
                    "label": label,
                    "name": loc.get("name", "Unknown"),
                    "lat": lat,
                    "lng": lng,
                    "color": color,
                    "google_maps_url": get_google_maps_url(
                        lat, lng, label=loc.get("name")
                    ),
                }
            )

    static_map_url = base_url + "&".join(params)

    return {
        "status": "success",
        "static_map_url": static_map_url,
        "map_type": map_type,
        "size": size,
        "marker_count": len(marker_data),
        "markers": marker_data,
        "message": "Click static_map_url to view the map image. Click individual google_maps_url links to open locations.",
    }


# =============================================================================
# Prompt Templates for Map Visualizations
# =============================================================================

MAP_VIZ_TEMPLATES = {
    "performance_map": {
        "infographic": """Create a professional business infographic map visualization.

=== REQUIRED LAYOUT (16:9 aspect ratio) ===
1. US MAP (occupies top 70% of image)
   - Continental United States outline with state borders
   - Dark blue ocean (#1a365d), light gray land (#e2e8f0)
   - Clean, modern cartographic style

2. CAMPAIGN MARKERS (on map at exact locations)
{markers_section}

3. LEGEND (bottom right corner, 15% width)
   - Title: "Campaign Performance"
   - Bubble size = Revenue (show scale: $10K, $30K, $50K)
   - Green circle = Active campaign
   - Gray circle = Draft campaign

4. SUMMARY PANEL (bottom left, 25% width)
   - Total Revenue: ${total_revenue:,.2f}
   - Total Impressions: {total_impressions:,}
   - Active Campaigns: {active_count}
   - Overall RPI: ${overall_rpi:.4f}

=== STYLE REQUIREMENTS ===
- Clean, flat design with subtle shadows
- Font: Sans-serif (Arial/Helvetica style)
- Colors: Professional blue (#2563eb), green (#22c55e), gray (#6b7280)
- White background for summary panel with light border
- High contrast text for readability

=== STRICT RULES ===
- Use ONLY the data values provided above
- Do NOT invent additional locations or numbers
- Bubbles must be positioned at the correct US locations
- Image must be exactly 16:9 aspect ratio""",
        "artistic": """Create a magazine-quality artistic map visualization.

=== ARTISTIC STYLE ===
- Editorial magazine aesthetic
- Watercolor or hand-drawn map style
- Elegant typography with serif headers
- Soft, sophisticated color palette (rose gold, navy, cream)

=== CONTENT REQUIREMENTS ===
1. STYLIZED US MAP with artistic treatment
{markers_section}

2. ELEGANT DATA CALLOUTS
   - Hand-lettered city labels
   - Decorative revenue indicators
   - Design-forward visual language

3. SUMMARY (integrated elegantly)
   - Total Revenue: ${total_revenue:,.2f}
   - Impressions: {total_impressions:,}

=== STRICT RULES ===
- Use ONLY provided data values
- Maintain 16:9 aspect ratio
- Professional yet artistic quality""",
        "simple": """Create a minimal, data-focused map.

=== MINIMAL LAYOUT ===
1. CLEAN US MAP with state outlines only
2. SIMPLE DOT MARKERS at locations:
{markers_section}

3. COMPACT LEGEND (small, corner)
4. DATA TABLE (bottom):
   | Location | Revenue | Impressions |
   {data_table}

=== STYLE ===
- Minimal design, maximum clarity
- Monochrome with single accent color
- Small, readable typography
- No decorative elements

Total: ${total_revenue:,.2f} revenue, {total_impressions:,} impressions
16:9 aspect ratio. Use ONLY provided data.""",
    },
    "regional_comparison": {
        "infographic": """Create a regional comparison dashboard.

=== LAYOUT ===
1. US MAP (top 50%) with regions color-coded:
   - West Coast: Blue (#3b82f6)
   - East Coast: Orange (#f97316)
   - Midwest: Green (#22c55e)
   - South: Purple (#a855f7)

2. BAR CHART (bottom 40%) comparing {metric}:
{regional_data}

3. INSIGHTS BOX (right side)
   - Best region highlighted with star
   - Percentage differences

=== STYLE ===
- Dashboard aesthetic
- Bold, clear typography
- High contrast bars
- 16:9 aspect ratio

Use ONLY the regional data provided above.""",
        "artistic": """Create an artistic regional performance visualization.

=== ARTISTIC STYLE ===
- Illustrated map with regional character
- Each region has distinct visual style
- Infographic elements with personality

=== REGIONAL DATA ===
{regional_data}

=== REQUIREMENTS ===
- Magazine-quality illustration
- 16:9 aspect ratio
- Use ONLY provided data""",
        "simple": """Create a simple regional comparison chart.

=== LAYOUT ===
1. Small US map with region colors
2. Clean bar chart:
{regional_data}

Minimal style. 16:9 ratio. Data only - no decoration.""",
    },
}


async def generate_map_visualization(
    visualization_type: str = "performance_map",
    metric: str = "revenue_per_impression",
    style: str = "infographic",
    tool_context: ToolContext | None = None,
) -> dict:
    """Generate a map-based visualization of campaign performance using Gemini 3 Pro Image.

    Creates professional geographic visualizations as images using AI image generation.
    The generated map is saved as an ADK artifact for viewing in the web UI.

    Args:
        visualization_type: Type of map visualization - one of:
            - performance_map: All campaigns on US map with metric bubbles
            - regional_comparison: Compare metrics by region (West/East/Midwest)
            - category_by_region: Product category performance by geography
            - market_opportunity: Current coverage vs expansion potential
            - campaign_heatmap: Revenue/density heatmap visualization
        metric: Metric to visualize - one of: revenue_per_impression, impressions, dwell_time, circulation
        style: Visual style - one of:
            - infographic: Clean business dashboard (default, best for presentations)
            - artistic: Magazine-quality editorial style
            - simple: Minimal, data-focused design
        tool_context: ADK ToolContext for artifact storage

    Returns:
        Dictionary with visualization details and artifact info
    """

    valid_types = [
        "performance_map",
        "regional_comparison",
        "category_by_region",
        "market_opportunity",
        "campaign_heatmap",
    ]
    valid_metrics = [
        "revenue_per_impression",
        "impressions",
        "dwell_time",
        "circulation",
    ]
    valid_styles = ["infographic", "artistic", "simple"]

    if visualization_type not in valid_types:
        return {
            "status": "error",
            "message": f"Invalid visualization_type. Must be one of: {', '.join(valid_types)}",
        }

    if metric not in valid_metrics:
        return {
            "status": "error",
            "message": f"Invalid metric. Must be one of: {', '.join(valid_metrics)}",
        }

    if style not in valid_styles:
        return {
            "status": "error",
            "message": f"Invalid style. Must be one of: {', '.join(valid_styles)}",
        }

    # Fetch all campaign data with metrics (in-store retail media metrics)
    # Uses NEW schema: video_metrics + campaign_videos (HITL workflow)
    with get_db_cursor() as cursor:
        cursor.execute("""
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
            GROUP BY c.id
            ORDER BY total_revenue DESC
        """)
        campaigns = cursor.fetchall()

    if not campaigns:
        return {
            "status": "error",
            "message": "No campaign data available for visualization",
        }

    # Process campaign data
    campaign_data = []
    regional_data: dict[str, dict[str, Any]] = {}
    category_data: dict[str, dict[str, Any]] = {}
    total_revenue = 0
    total_impressions = 0

    for camp in campaigns:
        location_key = f"{camp['city']}, {camp['state']}"
        coords = CITY_COORDINATES.get(location_key, {"lat": 39.8, "lng": -98.5})
        region = REGION_MAPPING.get(camp["state"], "Other")

        revenue = (
            round(camp["total_revenue"], 2) if camp["total_revenue"] else 0
        )
        impressions = (
            int(camp["total_impressions"]) if camp["total_impressions"] else 0
        )
        dwell_time = (
            round(camp["avg_dwell_time"], 1) if camp["avg_dwell_time"] else 0
        )
        circulation = (
            int(camp["total_circulation"]) if camp["total_circulation"] else 0
        )
        rpi = compute_rpi(revenue, impressions)

        total_revenue += revenue
        total_impressions += impressions

        camp_info = {
            "id": camp["id"],
            "name": camp["name"],
            "category": camp["category"],
            "city": camp["city"],
            "state": camp["state"],
            "location": location_key,
            "coords": coords,
            "region": region,
            "status": camp["status"],
            "video_count": camp["video_count"] or 0,
            "activated_videos": camp["activated_count"] or 0,
            "revenue": revenue,
            "impressions": impressions,
            "dwell_time": dwell_time,
            "circulation": circulation,
            "revenue_per_impression": rpi,
        }
        campaign_data.append(camp_info)

        # Aggregate by region
        if region not in regional_data:
            regional_data[region] = {
                "revenue": 0,
                "impressions": 0,
                "campaigns": 0,
                "dwell_rows": [],
                "circulation": 0,
            }
        regional_data[region]["revenue"] += revenue
        regional_data[region]["impressions"] += impressions
        regional_data[region]["campaigns"] += 1
        regional_data[region]["dwell_rows"].append(
            {"dwell_time": dwell_time, "impressions": impressions}
        )
        regional_data[region]["circulation"] += circulation

        # Aggregate by category
        cat = camp["category"] or "other"
        if cat not in category_data:
            category_data[cat] = {
                "revenue": 0,
                "impressions": 0,
                "campaigns": 0,
                "locations": [],
            }
        category_data[cat]["revenue"] += revenue
        category_data[cat]["impressions"] += impressions
        category_data[cat]["campaigns"] += 1
        category_data[cat]["locations"].append(location_key)

    # Calculate regional averages and RPI. Dwell is impressions-weighted:
    # an unweighted mean of per-campaign averages over-weights small
    # campaigns (see metrics_shared).
    for r_data in regional_data.values():
        if r_data["campaigns"] > 0:
            r_data["avg_dwell_time"] = round(
                compute_weighted_average(
                    r_data["dwell_rows"],
                    "dwell_time",
                    "impressions",
                ),
                1,
            )
            r_data["rpi"] = compute_rpi(
                r_data["revenue"],
                r_data["impressions"],
            )
        del r_data["dwell_rows"]

    # Build visualization prompt based on type

    if visualization_type == "performance_map":
        # Create location markers string
        markers_desc = ""
        for camp in campaign_data:
            status_color = "green" if camp["status"] == "active" else "gray"
            markers_desc += (
                f"- {camp['city']}, {camp['state']}: {camp['name']}\n"
            )
            markers_desc += f"  Revenue: ${camp['revenue']:,.2f} | Impressions: {camp['impressions']:,}\n"
            markers_desc += (
                f"  Status: {camp['status']} ({status_color} marker)\n"
            )

        visualization_prompt = f"""Create a professional, modern infographic map of the United States showing advertising campaign performance:

MAP SPECIFICATIONS:
- Style: Clean, modern business dashboard map visualization
- Geographic Scope: United States (continental)
- Theme: Dark blue ocean, light gray land with state borders

CAMPAIGN LOCATIONS TO MARK (with bubbles sized by revenue):
{markers_desc}

VISUAL ELEMENTS:
1. Each location gets a circular bubble marker
2. Bubble SIZE represents revenue (larger = more revenue)
3. Bubble COLOR:
   - Green = Active campaigns
   - Gray = Draft/Inactive campaigns
4. Each bubble has a small label with city name

DATA SUMMARY PANEL (bottom or side):
- Total Revenue: ${total_revenue:,.2f}
- Total Impressions: {total_impressions:,}
- Active Campaigns: {sum(1 for c in campaign_data if c["status"] == "active")}

STYLE REQUIREMENTS:
- Modern, flat design aesthetic
- Professional color palette (blues, greens, grays)
- Clean sans-serif typography
- Subtle drop shadows for depth
- Include a legend explaining bubble size = revenue

Create a high-quality, executive-ready map visualization suitable for business presentations."""

    elif visualization_type == "regional_comparison":
        # Build regional comparison data (in-store retail media metrics)
        region_desc = ""
        for region, data in sorted(
            regional_data.items(), key=lambda x: x[1]["revenue"], reverse=True
        ):
            region_desc += f"- {region}:\n"
            region_desc += f"  Revenue: ${data['revenue']:,.2f}\n"
            region_desc += f"  Impressions: {data['impressions']:,}\n"
            region_desc += f"  RPI: ${data.get('rpi', 0):.4f}\n"
            region_desc += f"  Campaigns: {data['campaigns']}\n"
            region_desc += (
                f"  Avg Dwell Time: {data.get('avg_dwell_time', 0):.1f}s\n"
            )

        visualization_prompt = f"""Create a professional regional comparison infographic showing advertising performance across US regions:

INFOGRAPHIC SPECIFICATIONS:
- Layout: US map with regions highlighted + comparison bar charts
- Regions: West Coast, East Coast, Midwest (each in different color)

REGIONAL DATA:
{region_desc}

VISUAL LAYOUT:
1. TOP SECTION: Stylized US map with regions color-coded
   - West Coast (California, Washington): Blue
   - East Coast (New York): Red/Orange
   - Midwest (Illinois): Green

2. BOTTOM SECTION: Horizontal bar chart comparison
   - Compare {metric} across all regions
   - Show actual values on bars
   - Rank from highest to lowest

3. KEY INSIGHTS BOX:
   - Best performing region highlighted
   - Percentage comparison between regions

STYLE:
- Modern dashboard aesthetic
- Bold, clear typography
- High contrast for readability
- Professional business visualization
- Include legend for colors

Create an executive summary view of regional advertising performance."""

    elif visualization_type == "category_by_region":
        # Build category performance data
        category_desc = ""
        for cat, data in sorted(
            category_data.items(), key=lambda x: x[1]["revenue"], reverse=True
        ):
            category_desc += f"- {cat.title()}:\n"
            category_desc += f"  Revenue: ${data['revenue']:,.2f}\n"
            category_desc += f"  Locations: {', '.join(data['locations'])}\n"

        visualization_prompt = f"""Create a professional infographic showing which product categories perform best in which geographic regions:

INFOGRAPHIC SPECIFICATIONS:
- Theme: Retail performance by location
- Style: Modern, editorial magazine quality

CATEGORY PERFORMANCE DATA:
{category_desc}

VISUAL LAYOUT:
1. STYLIZED US MAP with product category icons at each location:
   - Los Angeles: multiple product category icons
   - New York: multiple product category icons
   - Chicago: multiple product category icons
   - Seattle: multiple product category icons

2. CATEGORY CARDS (grid below map):
   Each card shows:
   - Category name with icon
   - Best performing location
   - Revenue in that category
   - Key metrics

3. INSIGHTS PANEL:
   - "Top categories vary by region"
   - "Performance concentrated in flagship locations"
   - Key regional preferences

STYLE REQUIREMENTS:
- Clean, editorial aesthetic
- Elegant typography (mix of serif and sans-serif)
- Soft, sophisticated color palette
- Include simple product category icons
- Magazine-quality layout

Create a beautiful visualization for retail strategy."""

    elif visualization_type == "market_opportunity":
        # Get demographic data for analysis
        demographics = {}
        for loc in [
            "Los Angeles, CA",
            "New York, NY",
            "Chicago, IL",
            "Seattle, WA",
        ]:
            city, state = loc.split(", ")
            demo_result = get_location_demographics(city, state)
            if demo_result["status"] == "success":
                demographics[loc] = demo_result["demographics"]

        opportunity_desc = ""
        for loc, demo in demographics.items():
            camp = next(
                (c for c in campaign_data if c["location"] == loc), None
            )
            current_revenue = camp["revenue"] if camp else 0
            market_index = demo.get("retail_market_index", 50)
            population = demo.get("population", "N/A")

            opportunity_desc += f"- {loc}:\n"
            opportunity_desc += f"  Population: {population:,} | Market Index: {market_index}/100\n"
            opportunity_desc += f"  Current Revenue: ${current_revenue:,.2f}\n"
            opportunity_desc += f"  Style Preferences: {', '.join(demo.get('style_preference', []))}\n"

        visualization_prompt = f"""Create a market opportunity map showing current campaign coverage versus expansion potential:

INFOGRAPHIC SPECIFICATIONS:
- Theme: Market expansion strategy visualization
- Style: Strategic planning dashboard

MARKET DATA:
{opportunity_desc}

VISUAL LAYOUT:
1. US MAP showing opportunity levels:
   - Current locations marked with solid circles
   - Opportunity level shown by halo/glow intensity
   - Green glow = high opportunity
   - Yellow glow = moderate opportunity

2. OPPORTUNITY SCORECARD (side panel):
   For each market:
   - Current revenue bar
   - Market potential bar (based on retail market index)
   - Gap = expansion opportunity

3. EXPANSION RECOMMENDATIONS:
   - Top 2 markets for growth
   - Underserved style categories
   - Population-based opportunity

4. KEY METRICS SUMMARY:
   - Total addressable market
   - Current market penetration
   - Growth potential %

STYLE:
- Strategic, data-driven aesthetic
- Color gradient from current (blue) to opportunity (green)
- Clean executive dashboard look
- Include growth arrow indicators

Create a strategic market opportunity visualization for expansion planning."""

    else:  # campaign_heatmap
        # Build heatmap data
        heatmap_desc = ""
        for camp in campaign_data:
            intensity = (
                "High"
                if camp["revenue"] > 30000
                else "Medium"
                if camp["revenue"] > 15000
                else "Low"
            )
            heatmap_desc += f"- {camp['location']}: {intensity} intensity (${camp['revenue']:,.2f})\n"

        visualization_prompt = f"""Create a heatmap visualization showing campaign revenue density across the United States:

INFOGRAPHIC SPECIFICATIONS:
- Style: Modern heatmap with glowing intensity
- Theme: Revenue concentration visualization

HEATMAP DATA POINTS:
{heatmap_desc}

VISUAL LAYOUT:
1. US MAP BASE:
   - Dark background for contrast
   - Subtle state borders

2. HEATMAP OVERLAY:
   - Glowing circles at each campaign location
   - Intensity (brightness/size) based on revenue
   - Color gradient: Blue (low) → Yellow (medium) → Red/Orange (high)
   - Soft glow/bloom effect for visual appeal

3. INTENSITY LEGEND:
   - Low: < $15,000 (blue, small glow)
   - Medium: $15,000 - $30,000 (yellow, medium glow)
   - High: > $30,000 (red/orange, large glow)

4. SUMMARY STATISTICS:
   - Total revenue: ${total_revenue:,.2f}
   - Campaign concentration areas
   - Revenue per region

STYLE:
- Dark mode dashboard aesthetic
- Neon/glowing effect for data points
- Modern data visualization style
- Include color scale legend

Create a visually striking revenue heatmap suitable for executive dashboards."""

    try:
        client = genai.Client()

        response = client.models.generate_content(
            model=IMAGE_GENERATION,
            contents=[visualization_prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio="16:9",
                ),
            ),
        )

        # Extract image from response
        generated_image = None
        for part in response.parts:
            if hasattr(part, "inline_data") and part.inline_data:
                generated_image = part
                break

        if generated_image is None:
            return {
                "status": "error",
                "message": "Failed to generate map visualization. Try a different visualization type.",
            }

        # Save as ADK artifact
        timestamp = int(time.time())
        filename = f"map_{visualization_type}_{style}_{metric}_{timestamp}.png"

        if tool_context:
            image_bytes = generated_image.inline_data.data
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
            "message": f"Generated {visualization_type} map visualization in {style} style",
            "visualization": {
                "type": visualization_type,
                "metric": metric,
                "style": style,
                "filename": filename,
                "artifact_saved": artifact_saved,
                "artifact_version": version,
            },
            "data_summary": {
                "campaigns_shown": len(campaign_data),
                "total_revenue": total_revenue,
                "total_impressions": total_impressions,
                "regions": list(regional_data.keys()),
                "categories": list(category_data.keys()),
            },
            "campaigns": [
                {
                    "name": c["name"],
                    "location": c["location"],
                    "revenue": c["revenue"],
                    "status": c["status"],
                }
                for c in campaign_data
            ],
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to generate map visualization: {e!s}",
        }
