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
"""Owner evidence lookup tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .shared import (
    JsonRecord,
    compact_address,
    is_valid_bbl,
    number_value,
    query_socrata,
    split_bbl,
    text,
)

NYC_DOMAIN = "data.cityofnewyork.us"
NYS_DOMAIN = "data.ny.gov"
PLUTO_DATASET = "64uk-42ks"
ACRIS_LEGALS_DATASET = "8h5j-fqxa"
ACRIS_MASTER_DATASET = "bnx9-e6tj"
ACRIS_PARTIES_DATASET = "636b-3b5g"
DOS_ACTIVE_CORPORATIONS_DATASET = "n9v6-gdp6"


def _empty_pluto_result(bbl: str, message: str) -> JsonRecord:
    return {
        "status": "error",
        "bbl": bbl,
        "ownerName": None,
        "address": None,
        "units": None,
        "yearBuilt": None,
        "source": "pluto",
        "records": [],
        "message": message,
    }


def _empty_acris_result(bbl: str, message: str) -> JsonRecord:
    return {
        "status": "error",
        "bbl": bbl,
        "documents": [],
        "source": "acris",
        "records": [],
        "message": message,
    }


def _normalize_entity_name(value: str) -> str:
    return " ".join(value.strip().upper().split())


def _get_property_owner_from_pluto(bbl: str) -> JsonRecord:
    try:
        records = query_socrata(
            NYC_DOMAIN, PLUTO_DATASET, {"bbl": bbl, "$limit": "1"}
        )
        if not records:
            return {
                **_empty_pluto_result(bbl, "No PLUTO record found for BBL."),
                "status": "not_found",
            }
        record = records[0]
        return {
            "status": "success",
            "bbl": bbl,
            "ownerName": text(record.get("ownername")),
            "address": text(record.get("address")),
            "units": number_value(record.get("unitstotal"))
            or number_value(record.get("unitsres")),
            "yearBuilt": number_value(record.get("yearbuilt")),
            "source": "pluto",
            "records": records,
            "message": "Resolved property owner evidence from PLUTO.",
        }
    except Exception as exc:
        return _empty_pluto_result(bbl, str(exc))


def _map_acris_party(record: JsonRecord) -> JsonRecord:
    return {
        "partyType": text(record.get("party_type")),
        "name": text(record.get("name")),
        "address": compact_address(
            [
                text(record.get("address_1")),
                text(record.get("address_2")),
                text(record.get("city")),
                text(record.get("state")),
                text(record.get("zip")),
            ]
        ),
    }


def _map_acris_document(
    master: JsonRecord | None, parties: list[JsonRecord], document_id: str
) -> JsonRecord:
    source = master or {}
    return {
        "documentId": document_id,
        "documentType": text(source.get("doc_type")),
        "documentDate": text(source.get("document_date")),
        "recordedDate": text(source.get("recorded_datetime")),
        "amount": text(source.get("document_amt")),
        "crfn": text(source.get("crfn")),
        "parties": [_map_acris_party(party) for party in parties],
    }


def _get_acris_records_by_bbl(bbl: str, limit: int = 5) -> JsonRecord:
    try:
        parts = split_bbl(bbl)
        bounded_limit = max(1, min(10, limit))
        legals = query_socrata(
            NYC_DOMAIN,
            ACRIS_LEGALS_DATASET,
            {
                **parts,
                "$limit": str(bounded_limit),
                "$order": "good_through_date DESC",
            },
        )
        document_ids = list(
            dict.fromkeys(
                document_id
                for record in legals
                if (document_id := text(record.get("document_id")))
            )
        )
        if not document_ids:
            return {
                "status": "not_found",
                "bbl": bbl,
                "documents": [],
                "source": "acris",
                "records": legals,
                "message": "No ACRIS legals records found for BBL.",
            }

        documents = []
        source_records: list[JsonRecord] = [*legals]
        for document_id in document_ids:
            masters = query_socrata(
                NYC_DOMAIN,
                ACRIS_MASTER_DATASET,
                {"document_id": document_id, "$limit": "1"},
            )
            parties = query_socrata(
                NYC_DOMAIN,
                ACRIS_PARTIES_DATASET,
                {"document_id": document_id, "$limit": "20"},
            )
            master = masters[0] if masters else None
            documents.append(_map_acris_document(master, parties, document_id))
            source_records.extend(masters)
            source_records.extend(parties)

        return {
            "status": "success",
            "bbl": bbl,
            "documents": documents,
            "source": "acris",
            "records": source_records,
            "message": "Resolved ACRIS document metadata and parties for BBL.",
        }
    except Exception as exc:
        return _empty_acris_result(bbl, str(exc))


def _map_dos_record(record: JsonRecord, match_type: str) -> JsonRecord:
    return {
        "currentEntityName": text(record.get("current_entity_name")),
        "dosId": text(record.get("dos_id")),
        "entityType": text(record.get("entity_type")),
        "filingDate": text(record.get("initial_dos_filing_date")),
        "jurisdiction": text(record.get("jurisdiction")),
        "serviceOfProcessName": text(record.get("dos_process_name")),
        "serviceOfProcessAddress": compact_address(
            [
                text(record.get("dos_process_address_1")),
                text(record.get("dos_process_address_2")),
                text(record.get("dos_process_city")),
                text(record.get("dos_process_state")),
                text(record.get("dos_process_zip")),
            ]
        ),
        "matchType": match_type,
    }


def _get_nys_dos_entity_by_name(entity_name: str) -> JsonRecord:
    normalized = _normalize_entity_name(entity_name)
    try:
        records = query_socrata(
            NYS_DOMAIN,
            DOS_ACTIVE_CORPORATIONS_DATASET,
            {
                "$where": f"upper(current_entity_name) = '{normalized}'",
                "$limit": "5",
            },
        )
        match_type = "exact"
        if not records:
            records = query_socrata(
                NYS_DOMAIN,
                DOS_ACTIVE_CORPORATIONS_DATASET,
                {
                    "$where": (
                        f"upper(current_entity_name) like '%{normalized}%'"
                    ),
                    "$limit": "5",
                },
            )
            match_type = "contains"
        if not records:
            return {
                "status": "not_found",
                "entityName": entity_name,
                "matches": [],
                "source": "dos",
                "records": records,
                "message": "No NYS DOS active corporation match found.",
            }
        return {
            "status": "success",
            "entityName": entity_name,
            "matches": [
                _map_dos_record(record, match_type) for record in records
            ],
            "source": "dos",
            "records": records,
            "message": "Resolved NYS DOS active corporation metadata.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "entityName": entity_name,
            "matches": [],
            "source": "dos",
            "records": [],
            "message": str(exc),
        }


def _build_evidence(
    pluto: JsonRecord,
    acris: JsonRecord,
    dos: JsonRecord | None,
    pulled_at: str,
) -> list[JsonRecord]:
    evidence = []
    if pluto.get("status") == "success":
        evidence.append(
            {
                "source": "pluto",
                "summary": (
                    "PLUTO lists owner "
                    f"{pluto.get('ownerName') or 'unknown'} for "
                    f"{pluto.get('address') or pluto.get('bbl')}."
                ),
                "recordId": pluto.get("bbl"),
                "pulledAt": pulled_at,
            }
        )
    if acris.get("status") == "success":
        documents = acris.get("documents")
        documents = documents if isinstance(documents, list) else []
        document_ids_text = ", ".join(
            text(document.get("documentId")) or ""
            for document in documents[:3]
            if isinstance(document, dict)
        ).strip(", ")
        evidence.append(
            {
                "source": "acris",
                "summary": (
                    f"ACRIS returned {len(documents)} document metadata "
                    "record(s) for the BBL"
                    f"{', including ' + document_ids_text if document_ids_text else ''}."
                ),
                "recordId": (
                    documents[0].get("documentId") if documents else None
                ),
                "pulledAt": pulled_at,
            }
        )
    if dos and dos.get("status") == "success":
        matches = dos.get("matches")
        matches = matches if isinstance(matches, list) else []
        first = matches[0] if matches else {}
        evidence.append(
            {
                "source": "dos",
                "summary": (
                    "NYS DOS returned "
                    f"{first.get('matchType') or 'unknown'} active entity "
                    "match "
                    f"{first.get('currentEntityName') or dos.get('entityName')}."
                ),
                "recordId": first.get("dosId"),
                "pulledAt": pulled_at,
            }
        )
    return evidence


def _confidence_for(
    pluto: JsonRecord, acris: JsonRecord, dos: JsonRecord | None
) -> str:
    matches = dos.get("matches") if dos else []
    matches = matches if isinstance(matches, list) else []
    if pluto.get("status") == "success" and any(
        isinstance(match, dict) and match.get("matchType") == "exact"
        for match in matches
    ):
        return "high"
    if pluto.get("status") == "success" and acris.get("status") == "success":
        return "high"
    if pluto.get("status") == "success" or acris.get("status") == "success":
        return "medium"
    if (
        pluto.get("status") == "error"
        or acris.get("status") == "error"
        or (dos and dos.get("status") == "error")
    ):
        return "low"
    return "unknown"


def find_owner_by_bbl(bbl: str) -> dict[str, Any]:
    """Find owner evidence for one 10-digit NYC BBL.

    Args:
        bbl: A 10-digit NYC Borough-Block-Lot identifier.

    Returns:
        Structured ownership evidence from public records.
    """
    if not is_valid_bbl(bbl):
        return {
            "status": "needs_more_info",
            "bbl": bbl,
            "ownerEntity": None,
            "connectedPeople": [],
            "confidence": "unknown",
            "evidence": [],
            "sources": {
                "pluto": _empty_pluto_result(
                    bbl, "BBL must be exactly 10 digits."
                ),
                "acris": _empty_acris_result(
                    bbl, "BBL must be exactly 10 digits."
                ),
                "dos": None,
            },
            "message": "A 10-digit NYC BBL is required for owner lookup.",
        }

    pluto = _get_property_owner_from_pluto(bbl)
    acris = _get_acris_records_by_bbl(bbl)
    owner_entity = text(pluto.get("ownerName"))
    dos = _get_nys_dos_entity_by_name(owner_entity) if owner_entity else None
    pulled_at = datetime.now(UTC).isoformat()
    evidence = _build_evidence(pluto, acris, dos, pulled_at)

    return {
        "status": "found" if owner_entity or evidence else "not_found",
        "bbl": bbl,
        "ownerEntity": owner_entity,
        "connectedPeople": [],
        "confidence": _confidence_for(pluto, acris, dos),
        "evidence": evidence,
        "sources": {"pluto": pluto, "acris": acris, "dos": dos},
        "message": (
            "Resolved owner evidence from verified public records."
            if owner_entity
            else "No owner entity was resolved from verified public records."
        ),
    }
