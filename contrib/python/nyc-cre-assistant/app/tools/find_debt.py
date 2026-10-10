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
"""Recorded mortgage/debt evidence lookup tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .shared import (
    JsonRecord,
    compact_address,
    is_valid_bbl,
    query_socrata,
    split_bbl,
    text,
)

NYC_DOMAIN = "data.cityofnewyork.us"
ACRIS_LEGALS_DATASET = "8h5j-fqxa"
ACRIS_MASTER_DATASET = "bnx9-e6tj"
ACRIS_PARTIES_DATASET = "636b-3b5g"
DEBT_DOCUMENT_TYPES = {"MTGE", "ASST", "SAT", "REL", "AGMT", "M&CON"}


def _empty_acris_debt_result(bbl: str, message: str) -> JsonRecord:
    return {
        "status": "error",
        "bbl": bbl,
        "documents": [],
        "records": [],
        "message": message,
    }


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
    master: JsonRecord, parties: list[JsonRecord]
) -> JsonRecord:
    return {
        "documentId": text(master.get("document_id")),
        "documentType": text(master.get("doc_type")),
        "documentDate": text(master.get("document_date")),
        "recordedDate": text(master.get("recorded_datetime")),
        "recordedAmount": text(master.get("document_amt")),
        "crfn": text(master.get("crfn")),
        "parties": [_map_acris_party(party) for party in parties],
    }


def _get_acris_debt_records_by_bbl(bbl: str, limit: int = 30) -> JsonRecord:
    try:
        parts = split_bbl(bbl)
        bounded_limit = max(1, min(50, limit))
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
                "records": legals,
                "message": "No ACRIS legals records found for BBL.",
            }

        debt_documents = []
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
            if not masters:
                continue
            master = masters[0]
            document_type = text(master.get("doc_type"))
            if not document_type or document_type not in DEBT_DOCUMENT_TYPES:
                continue
            debt_documents.append(_map_acris_document(master, parties))
            source_records.extend(masters)
            source_records.extend(parties)

        if not debt_documents:
            return {
                "status": "not_found",
                "bbl": bbl,
                "documents": [],
                "records": source_records,
                "message": (
                    "No mortgage/debt-related ACRIS metadata found for BBL."
                ),
            }

        return {
            "status": "success",
            "bbl": bbl,
            "documents": debt_documents,
            "records": source_records,
            "message": "Resolved ACRIS mortgage/debt metadata for BBL.",
        }
    except Exception as exc:
        return _empty_acris_debt_result(bbl, str(exc))


def _party_names_by_type(document: JsonRecord, party_type: str) -> list[str]:
    parties = document.get("parties")
    parties = parties if isinstance(parties, list) else []
    return [
        str(party["name"])
        for party in parties
        if isinstance(party, dict)
        and party.get("partyType") == party_type
        and party.get("name")
    ]


def _build_evidence(acris: JsonRecord, pulled_at: str) -> list[JsonRecord]:
    if acris.get("status") != "success":
        return []
    documents = acris.get("documents")
    documents = documents if isinstance(documents, list) else []
    document_types = sorted(
        {
            document_type
            for document in documents
            if isinstance(document, dict)
            and (document_type := text(document.get("documentType")))
        }
    )
    return [
        {
            "source": "acris",
            "summary": (
                f"ACRIS returned {len(documents)} mortgage/debt-related "
                "document(s)"
                f"{' with type(s): ' + ', '.join(document_types) if document_types else ''}."
            ),
            "recordId": documents[0].get("documentId") if documents else None,
            "pulledAt": pulled_at,
        }
    ]


def _confidence_for(acris: JsonRecord) -> str:
    if acris.get("status") != "success":
        return "low" if acris.get("status") == "error" else "unknown"
    documents = acris.get("documents")
    documents = documents if isinstance(documents, list) else []
    has_parties = any(
        isinstance(document, dict)
        and isinstance(document.get("parties"), list)
        and bool(document["parties"])
        for document in documents
    )
    return "high" if has_parties else "medium"


def find_debt_by_bbl(bbl: str) -> dict[str, Any]:
    """Find recorded mortgage/debt evidence for one 10-digit NYC BBL.

    Args:
        bbl: A 10-digit NYC Borough-Block-Lot identifier.

    Returns:
        Structured ACRIS document evidence. The result never claims current
        outstanding balance, payoff status, or actual maturity.
    """
    if not is_valid_bbl(bbl):
        acris = _empty_acris_debt_result(bbl, "BBL must be exactly 10 digits.")
        return {
            "status": "needs_more_info",
            "bbl": bbl,
            "recordedDebt": [],
            "currentDebtKnown": False,
            "maturityKnown": False,
            "confidence": "unknown",
            "evidence": [],
            "sources": {"acris": acris},
            "message": "A 10-digit NYC BBL is required for debt lookup.",
        }

    acris = _get_acris_debt_records_by_bbl(bbl)
    pulled_at = datetime.now(UTC).isoformat()
    documents = acris.get("documents")
    documents = documents if isinstance(documents, list) else []
    recorded_debt = [
        {
            "documentId": document.get("documentId"),
            "documentType": document.get("documentType"),
            "documentDate": document.get("documentDate"),
            "recordedDate": document.get("recordedDate"),
            "recordedAmount": document.get("recordedAmount"),
            "crfn": document.get("crfn"),
            "lendersOrMortgagees": _party_names_by_type(document, "2"),
            "borrowersOrMortgagors": _party_names_by_type(document, "1"),
            "parties": document.get("parties", []),
        }
        for document in documents
        if isinstance(document, dict)
    ]

    return {
        "status": "found" if recorded_debt else "not_found",
        "bbl": bbl,
        "recordedDebt": recorded_debt,
        "currentDebtKnown": False,
        "maturityKnown": False,
        "confidence": _confidence_for(acris),
        "evidence": _build_evidence(acris, pulled_at),
        "sources": {"acris": acris},
        "message": (
            "Resolved recorded mortgage/debt evidence from ACRIS metadata. "
            "Current balance and maturity are not known from metadata alone."
            if recorded_debt
            else "No recorded mortgage/debt evidence was resolved from ACRIS "
            "metadata."
        ),
    }
