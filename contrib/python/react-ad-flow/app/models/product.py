"""Generic product model — the vertical-agnostic representation of a product.

Backed by the products table: typed core columns plus the metadata JSON
column, which carries all vertical-specific attributes (fashion's
style/color/fabric/occasion, a beverage's flavor/volume, ...) with no fixed
schema by design (Phase 8). Phase 15's product CRUD writes through to_row().
"""

import json
from typing import Any

from pydantic import BaseModel, Field

# products columns that map to typed Product fields (or storage internals) —
# anything else found in the metadata JSON belongs in `attributes`.
_CORE_ROW_KEYS = {
    "id",
    "name",
    "category",
    "details",
    "description",
    "image_filename",
    "gcs_path",
    "local_path",
    "metadata",
    "created_at",
}
# Legacy fashion columns: still real columns for the seeded catalog, but
# exposed uniformly through `attributes` so consumers have one access path.
_LEGACY_ATTRIBUTE_COLUMNS = ("style", "color", "fabric", "occasion")


class Product(BaseModel):
    """A product advertisable on in-store screens — any retail vertical."""

    id: int | None = None
    name: str
    category: str = ""
    description: str = ""  # maps to the products.details column
    image_filename: str
    gcs_path: str | None = None
    local_path: str | None = None
    created_at: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict) -> "Product":
        """Build a Product from a products-table row dict.

        metadata JSON keys become `attributes` (minus core-column
        duplicates — both seeders dump the whole entry dict); the legacy
        fashion columns then fold in on top (row value wins), so every
        historical row shape produces the same attributes.
        """
        raw_meta = row.get("metadata")
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        attributes = {
            key: value
            for key, value in meta.items()
            if key not in _CORE_ROW_KEYS and value is not None
        }
        for column in _LEGACY_ATTRIBUTE_COLUMNS:
            if row.get(column) is not None:
                attributes[column] = row[column]
        return cls(
            id=row.get("id"),
            name=row["name"],
            category=row.get("category") or "",
            description=row.get("details") or "",
            image_filename=row["image_filename"],
            gcs_path=row.get("gcs_path"),
            local_path=row.get("local_path"),
            created_at=str(row["created_at"])
            if row.get("created_at") is not None
            else None,
            attributes=attributes,
        )

    def to_row(self) -> dict:
        """Column dict for INSERT INTO products — the from_row round-trip.

        Legacy fashion attributes land back in their typed columns; the
        whole attributes dict is serialized into metadata, so nothing is
        lost in either direction.
        """
        return {
            "name": self.name,
            "category": self.category or None,
            "style": self.attributes.get("style"),
            "color": self.attributes.get("color"),
            "fabric": self.attributes.get("fabric"),
            "occasion": self.attributes.get("occasion"),
            "details": self.description or None,
            "image_filename": self.image_filename,
            "gcs_path": self.gcs_path,
            "local_path": self.local_path,
            "metadata": json.dumps(self.attributes),
        }
