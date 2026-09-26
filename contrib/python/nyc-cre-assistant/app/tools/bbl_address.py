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
"""Address normalization and BBL lookup tool."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

from .shared import JsonRecord, query_json, text

BOROUGHS = {"Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"}


def _field(record: JsonRecord, key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


def _query_geoclient(
    house_number: str, street: str, borough: str
) -> JsonRecord | None:
    api_key = os.getenv("GEOCLIENT_V2_PK", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEOCLIENT_V2_PK for NYC Geoclient lookup.")

    query = urlencode(
        {"houseNumber": house_number, "street": street, "borough": borough}
    )
    data = query_json(
        f"https://api.nyc.gov/geoclient/v2/address?{query}",
        {"Ocp-Apim-Subscription-Key": api_key},
    )
    if not isinstance(data, dict):
        return None
    address = data.get("address")
    return address if isinstance(address, dict) else data


def get_bbl_from_normalized_address(
    house_number: str, street: str, borough: str
) -> dict[str, Any]:
    """Resolve a BBL from one normalized NYC address.

    Args:
        house_number: Street house number, for example "200".
        street: Street name, for example "Park Avenue".
        borough: NYC borough name.

    Returns:
        A structured result with normalized address fields, BBL, source, and
        error information when the lookup cannot be resolved.
    """
    normalized_house_number = house_number.strip()
    normalized_street = street.strip()
    normalized_borough = borough.strip()

    if normalized_borough not in BOROUGHS:
        return {
            "status": "error",
            "outcome": "unsupported",
            "houseNumber": normalized_house_number,
            "street": normalized_street,
            "borough": normalized_borough,
            "message": "Borough must be one of the five NYC borough names.",
        }

    try:
        geoclient = _query_geoclient(
            normalized_house_number,
            normalized_street,
            normalized_borough,
        )
        bbl = _field(geoclient, "bbl") if geoclient else ""
        bin_value = _field(geoclient, "buildingIdentificationNumber")

        if not bbl:
            return {
                "status": "error",
                "outcome": "unresolved",
                "houseNumber": normalized_house_number,
                "street": normalized_street,
                "borough": normalized_borough,
                "message": "Unable to resolve a BBL from Geoclient.",
            }

        record = geoclient or {}
        return {
            "status": "success",
            "outcome": "resolved",
            "houseNumber": _field(record, "houseNumber")
            or normalized_house_number,
            "street": _field(record, "firstStreetNameNormalized")
            or normalized_street,
            "borough": _field(record, "boroughCodeName") or normalized_borough,
            "bbl": bbl.replace("-", ""),
            "bin": text(bin_value),
            "source": "geoclient",
            "message": "Resolved BBL from NYC Geoclient.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "outcome": "unresolved",
            "houseNumber": normalized_house_number,
            "street": normalized_street,
            "borough": normalized_borough,
            "message": str(exc),
        }
