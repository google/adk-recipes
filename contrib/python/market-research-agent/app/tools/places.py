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

"""Google Places API (v1) + Geocoding API tools for ADK agents."""

import json
import os

import httpx

_PLACES_BASE_URL = "https://places.googleapis.com/v1"
_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_PRICE_LEVEL_MAP = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


def _api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY environment variable is not set."
        )
    return key


def _normalize_place(p: dict) -> dict:
    """Flatten nested Places API v1 response into a flat dict."""
    loc = p.get("location", {})
    display_name = p.get("displayName", {})
    price_raw = p.get("priceLevel")
    # Use the full resource name (e.g. "places/ChIJ...") so place_details
    # can pass it directly as a URL path segment.
    resource_name = p.get("name", "")
    return {
        "place_id": resource_name,
        "name": (
            display_name.get("text", "")
            if isinstance(display_name, dict)
            else display_name
        ),
        "address": p.get("formattedAddress", ""),
        "lat": loc.get("latitude", 0.0),
        "lng": loc.get("longitude", 0.0),
        "rating": p.get("rating"),
        "user_ratings_total": p.get("userRatingCount", 0),
        "price_level": _PRICE_LEVEL_MAP.get(price_raw) if price_raw else None,
        "types": p.get("types", []),
        "opening_hours": p.get("currentOpeningHours", {}),
        "reviews": p.get("reviews", []),
    }


async def geocode_address(address: str) -> str:
    """Convert a human-readable address into latitude/longitude coordinates.

    Args:
        address: Full address to geocode, e.g. '15 Shoreditch High St, London'.

    Returns:
        JSON string with lat, lng, and formatted_address fields.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _GEOCODING_URL,
            params={"address": address, "key": _api_key()},
        )
        resp.raise_for_status()
        data = resp.json()

    if data["status"] != "OK":
        return json.dumps(
            {
                "error": f"Geocoding failed: {data['status']}",
                "details": data.get("error_message", ""),
            }
        )

    result = data["results"][0]
    loc = result["geometry"]["location"]
    return json.dumps(
        {
            "lat": loc["lat"],
            "lng": loc["lng"],
            "formatted_address": result["formatted_address"],
        }
    )


async def nearby_search(
    lat: float,
    lng: float,
    business_type: str,
    radius_meters: int = 1000,
    max_results: int = 20,
) -> str:
    """Search for businesses of a given type near a lat/lng coordinate.

    Uses Google Places Nearby Search to find businesses within a radius.

    Args:
        lat: Latitude of the search center.
        lng: Longitude of the search center.
        business_type: Google Places type string, e.g. 'cafe', 'gym',
            'restaurant', 'supermarket', 'transit_station'. Spaces are
            automatically converted to underscores.
        radius_meters: Search radius in meters (default 1000, max 50000).
        max_results: Maximum number of results to return (max 20).

    Returns:
        JSON string containing a list of places with id, name, address,
        rating, price_level, and location.
    """
    # Places API requires underscore-separated type strings, not spaces
    business_type = business_type.lower().replace(" ", "_").replace("-", "_")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_PLACES_BASE_URL}/places:searchNearby",
                json={
                    "includedTypes": [business_type],
                    "maxResultCount": min(max_results, 20),
                    "locationRestriction": {
                        "circle": {
                            "center": {"latitude": lat, "longitude": lng},
                            "radius": float(radius_meters),
                        }
                    },
                },
                headers={
                    "X-Goog-Api-Key": _api_key(),
                    "X-Goog-FieldMask": (
                        "places.name,places.id,places.displayName,"
                        "places.formattedAddress,places.rating,"
                        "places.userRatingCount,places.priceLevel,"
                        "places.location,places.types"
                    ),
                },
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return json.dumps(
            {
                "error": f"Places API error {e.response.status_code}",
                "details": e.response.text,
            }
        )

    places = resp.json().get("places", [])
    return json.dumps([_normalize_place(p) for p in places])


async def place_details(place_id: str) -> str:
    """Get detailed information about a specific place.

    Retrieves reviews, opening hours, price level, and rating for a place.

    Args:
        place_id: The place ID returned by nearby_search or text_search.

    Returns:
        JSON string with rating, reviews, opening_hours, price_level,
        and other place details.
    """
    fields = [
        "rating",
        "reviews",
        "currentOpeningHours",
        "priceLevel",
        "userRatingCount",
        "displayName",
        "formattedAddress",
        "types",
        "location",
    ]
    # place_id is the full resource name "places/ChIJ..." — use it directly
    # as the URL path after the base (e.g. .../v1/places/ChIJ...)
    resource = (
        place_id if place_id.startswith("places/") else f"places/{place_id}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_PLACES_BASE_URL}/{resource}",
            headers={
                "X-Goog-Api-Key": _api_key(),
                "X-Goog-FieldMask": ",".join(fields),
            },
        )
        resp.raise_for_status()

    return json.dumps(_normalize_place(resp.json()))


async def text_search(
    query: str,
    lat: float,
    lng: float,
    radius_meters: int = 1500,
) -> str:
    """Search for businesses using a free-text query near a location.

    Args:
        query: Search query, e.g. 'specialty coffee Shoreditch' or
            'vegan cafe'.
        lat: Latitude of the search center.
        lng: Longitude of the search center.
        radius_meters: Search radius in meters (default 1500).

    Returns:
        JSON string containing a list of matching places.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_PLACES_BASE_URL}/places:searchText",
            json={
                "textQuery": query,
                "locationBias": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": float(radius_meters),
                    }
                },
                "maxResultCount": 20,
            },
            headers={
                "X-Goog-Api-Key": _api_key(),
                "X-Goog-FieldMask": (
                    "places.name,places.id,places.displayName,"
                    "places.formattedAddress,places.rating,"
                    "places.userRatingCount,places.priceLevel,"
                    "places.types,places.location"
                ),
            },
        )
        resp.raise_for_status()

    places = resp.json().get("places", [])
    return json.dumps([_normalize_place(p) for p in places])
