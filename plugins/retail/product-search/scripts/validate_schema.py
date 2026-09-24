#!/usr/bin/env python3
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

"""Validate a product catalog CSV/JSON against the expected schema.

Usage:
    python validate_schema.py --file products.csv --fields-level Standard
    python validate_schema.py --file products.json --fields-level Extended
"""

# pylint: disable=line-too-long
# (validation error messages with field names are intentionally long.)

import argparse
import csv
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FIELD_LEVELS = {
    "Basic": {
        "required": ["product_id", "name", "price", "description"],
        "optional": [],
    },
    "Standard": {
        "required": ["product_id", "name", "price", "description"],
        "optional": ["category", "brand", "image_url"],
    },
    "Extended": {
        "required": ["product_id", "name", "price", "description"],
        "optional": [
            "category",
            "brand",
            "image_url",
            "rating",
            "stock",
            "manufacturer",
        ],
    },
    "Full": {
        "required": ["product_id", "name", "price", "description"],
        "optional": [
            "category",
            "brand",
            "image_url",
            "rating",
            "stock",
            "manufacturer",
            "variants",
            "tags",
            "specifications",
            "reviews",
        ],
    },
}


def load_records(file_path: pathlib.Path) -> list[dict[str, Any]]:
    """Load records from a CSV, JSON, or JSONL file.

    Args:
        file_path: Path to the product data file. The file type is
            inferred from the suffix (``.csv``, ``.json``, or ``.jsonl``).

    Returns:
        List of records as dicts.

    Raises:
        ValueError: If the suffix isn't supported, or the JSON content is
            neither a list nor a ``{"products": [...]}`` object.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        with open(file_path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    elif suffix in (".json", ".jsonl"):
        content = file_path.read_text(encoding="utf-8")
        if suffix == ".jsonl":
            return [
                json.loads(line)
                for line in content.strip().splitlines()
                if line.strip()
            ]
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "products" in parsed:
            return parsed["products"]
        raise ValueError('JSON must be an array or {"products": [...]}')
    else:
        raise ValueError(
            f"Unsupported file type: {suffix}. Use .csv, .json, or .jsonl"
        )


def _validate_single_record(
    record: dict[str, Any],
    row_num: int,
    required: list[str],
    all_fields: set[str],
    higher_level_fields: set[str],
    seen_warned_fields: set[str],
    fields_level: str,
) -> list[str]:
    """Validate a single record against the schema.

    Args:
        record: One product record as a dict from CSV/JSON.
        row_num: 1-based row number for error messages.
        required: Required field names at the selected fields-level.
        all_fields: Set of fields recognized at the selected fields-level.
        higher_level_fields: Fields recognized at any higher level; these
            warn (and are ignored) instead of failing the row.
        seen_warned_fields: Mutable set tracking which higher-level fields
            already produced a warning, so each fires once per run.
        fields_level: Name of the selected level, used in warning messages.

    Returns:
        List of error strings for this row (empty if the row is valid).
    """
    row_errors = []

    for field in required:
        if field not in record or not record[field]:
            row_errors.append(
                f"Row {row_num}: missing required field '{field}'"
            )

    if record.get("price"):
        try:
            float(record["price"])
        except (ValueError, TypeError):
            row_errors.append(
                f"Row {row_num}: 'price' must be numeric, got '{record['price']}'"
            )

    if record.get("rating"):
        try:
            val = float(record["rating"])
            if not 0 <= val <= 5:
                row_errors.append(
                    f"Row {row_num}: 'rating' should be 0-5, got {val}"
                )
        except (ValueError, TypeError):
            row_errors.append(f"Row {row_num}: 'rating' must be numeric")

    if record.get("stock"):
        try:
            int(record["stock"])
        except (ValueError, TypeError):
            row_errors.append(f"Row {row_num}: 'stock' must be an integer")

    extra_fields = set(record.keys()) - all_fields
    known_extras = extra_fields & higher_level_fields
    unknown_extras = extra_fields - higher_level_fields

    for field in known_extras - seen_warned_fields:
        logger.warning(
            "Field '%s' present in data but outside the '%s' schema. "
            "It will be ignored. Pick a higher --fields-level if you want it indexed.",
            field,
            fields_level,
        )
        seen_warned_fields.add(field)

    if unknown_extras:
        row_errors.append(
            f"Row {row_num}: unrecognized fields: {sorted(unknown_extras)}"
        )

    return row_errors


def validate(
    records: list[dict[str, Any]], fields_level: str
) -> tuple[int, list[str]]:
    """Validate records against the schema for ``fields_level``.

    Args:
        records: All product records loaded from the source.
        fields_level: One of ``"Basic"``, ``"Standard"``, ``"Extended"``,
            ``"Full"``.

    Returns:
        ``(valid_count, errors)`` where ``valid_count`` is the number of
        rows that passed and ``errors`` is the concatenated list of error
        strings across all rows.
    """
    schema = FIELD_LEVELS[fields_level]
    required = schema["required"]
    all_fields = set(required + schema["optional"])
    # Fields that exist at any higher level -- treat as known-but-out-of-scope.
    # Warn instead of erroring so users with richer CSVs don't have to strip columns.
    higher_level_fields: set[str] = set()
    for level_schema in FIELD_LEVELS.values():
        higher_level_fields.update(level_schema["required"])
        higher_level_fields.update(level_schema["optional"])

    errors = []
    seen_warned_fields: set[str] = set()
    valid = 0

    for i, record in enumerate(records, start=1):
        row_errors = _validate_single_record(
            record,
            i,
            required,
            all_fields,
            higher_level_fields,
            seen_warned_fields,
            fields_level,
        )
        if row_errors:
            errors.extend(row_errors)
        else:
            valid += 1

    return valid, errors


def validate_file(file_path, fields_level: str) -> tuple[int, list[str]]:
    """Load a CSV/JSON file and validate it in one call.

    Convenience wrapper for orchestrators that don't need the records.

    Args:
        file_path: Path to the product data file (CSV, JSON, or JSONL).
        fields_level: One of ``"Basic"``, ``"Standard"``, ``"Extended"``,
            ``"Full"``.

    Returns:
        ``(valid_count, errors)`` -- same shape as ``validate()``.
    """
    records = load_records(pathlib.Path(file_path))
    return validate(records, fields_level)


def main():
    """Parse CLI arguments and run schema validation."""
    parser = argparse.ArgumentParser(
        description="Validate product catalog schema"
    )
    parser.add_argument(
        "--file", required=True, help="Path to product data file (CSV/JSON)"
    )
    parser.add_argument(
        "--fields-level",
        choices=["Basic", "Standard", "Extended", "Full"],
        default="Standard",
        help="Product fields level (default: Standard)",
    )

    args = parser.parse_args()
    file_path = pathlib.Path(args.file)

    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        sys.exit(1)

    records = load_records(file_path)
    logger.info("Loaded %d records from %s", len(records), file_path)

    valid_count, errors = validate(records, args.fields_level)

    if errors:
        logger.warning("%d validation errors found:", len(errors))
        for err in errors[:20]:
            logger.warning("  %s", err)
        if len(errors) > 20:
            logger.warning("  ... and %d more", len(errors) - 20)

    logger.info(
        "Validation complete: %d/%d records valid", valid_count, len(records)
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
