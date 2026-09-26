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
"""Offline tests for deterministic public-record tools."""

from __future__ import annotations

from typing import Any

from app.tools import bbl_address, find_debt, find_owner
from app.tools.shared import split_bbl


def test_split_bbl() -> None:
    assert split_bbl("1015410021") == {
        "borough": "1",
        "block": "1541",
        "lot": "21",
    }


def test_get_bbl_from_normalized_address(monkeypatch: Any) -> None:
    monkeypatch.setenv("GEOCLIENT_V2_PK", "test-key")

    def fake_query_geoclient(
        house_number: str, street: str, borough: str
    ) -> dict[str, str]:
        assert house_number == "1849"
        assert street == "2nd Ave"
        assert borough == "Manhattan"
        return {
            "houseNumber": "1849",
            "firstStreetNameNormalized": "2 AVENUE",
            "boroughCodeName": "Manhattan",
            "bbl": "1015410021",
            "buildingIdentificationNumber": "1071234",
        }

    monkeypatch.setattr(bbl_address, "_query_geoclient", fake_query_geoclient)

    result = bbl_address.get_bbl_from_normalized_address(
        house_number="1849",
        street="2nd Ave",
        borough="Manhattan",
    )

    assert result["status"] == "success"
    assert result["outcome"] == "resolved"
    assert result["bbl"] == "1015410021"
    assert result["bin"] == "1071234"
    assert result["source"] == "geoclient"


def test_get_bbl_rejects_non_nyc_borough() -> None:
    result = bbl_address.get_bbl_from_normalized_address(
        house_number="35",
        street="Broadway",
        borough="Albany",
    )

    assert result["status"] == "error"
    assert result["outcome"] == "unsupported"


def test_find_owner_by_bbl_aggregates_public_evidence(monkeypatch: Any) -> None:
    def fake_pluto(bbl: str) -> dict[str, Any]:
        return {
            "status": "success",
            "bbl": bbl,
            "ownerName": "MF ASSOCIATES OF NEW YORK LLC",
            "address": "1849 2 AVENUE",
            "records": [],
        }

    def fake_acris(bbl: str) -> dict[str, Any]:
        return {
            "status": "success",
            "bbl": bbl,
            "documents": [{"documentId": "2020123000566004"}],
            "records": [],
        }

    def fake_dos(entity_name: str) -> dict[str, Any]:
        assert entity_name == "MF ASSOCIATES OF NEW YORK LLC"
        return {
            "status": "success",
            "entityName": entity_name,
            "matches": [
                {
                    "currentEntityName": entity_name,
                    "dosId": "1234567",
                    "matchType": "exact",
                }
            ],
            "records": [],
        }

    monkeypatch.setattr(
        find_owner, "_get_property_owner_from_pluto", fake_pluto
    )
    monkeypatch.setattr(find_owner, "_get_acris_records_by_bbl", fake_acris)
    monkeypatch.setattr(find_owner, "_get_nys_dos_entity_by_name", fake_dos)

    result = find_owner.find_owner_by_bbl("1015410021")

    assert result["status"] == "found"
    assert result["ownerEntity"] == "MF ASSOCIATES OF NEW YORK LLC"
    assert result["connectedPeople"] == []
    assert result["confidence"] == "high"
    assert {item["source"] for item in result["evidence"]} == {
        "pluto",
        "acris",
        "dos",
    }


def test_find_owner_requires_valid_bbl() -> None:
    result = find_owner.find_owner_by_bbl("bad-bbl")

    assert result["status"] == "needs_more_info"
    assert result["confidence"] == "unknown"
    assert result["ownerEntity"] is None


def test_find_debt_by_bbl_maps_lender_and_borrower(monkeypatch: Any) -> None:
    def fake_acris_debt(bbl: str) -> dict[str, Any]:
        return {
            "status": "success",
            "bbl": bbl,
            "documents": [
                {
                    "documentId": "2020123000566004",
                    "documentType": "MTGE",
                    "recordedAmount": "25000000",
                    "parties": [
                        {"partyType": "2", "name": "JPMORGAN CHASE BANK, NA"},
                        {
                            "partyType": "1",
                            "name": "MF ASSOCIATES OF NEW YORK LLC",
                        },
                    ],
                }
            ],
            "records": [],
        }

    monkeypatch.setattr(
        find_debt, "_get_acris_debt_records_by_bbl", fake_acris_debt
    )

    result = find_debt.find_debt_by_bbl("1015410021")

    assert result["status"] == "found"
    assert result["currentDebtKnown"] is False
    assert result["maturityKnown"] is False
    assert result["confidence"] == "high"
    assert result["recordedDebt"][0]["lendersOrMortgagees"] == [
        "JPMORGAN CHASE BANK, NA"
    ]
    assert result["recordedDebt"][0]["borrowersOrMortgagors"] == [
        "MF ASSOCIATES OF NEW YORK LLC"
    ]


def test_find_debt_requires_valid_bbl() -> None:
    result = find_debt.find_debt_by_bbl("101")

    assert result["status"] == "needs_more_info"
    assert result["recordedDebt"] == []
    assert result["currentDebtKnown"] is False
