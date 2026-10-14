---
name: bbl-address
description: Use when the user gives one NYC address and wants a deterministic BBL lookup.
---

# BBL Address

## Purpose

Normalize one New York City building address when possible, call
`get_bbl_from_normalized_address`, and return the resolved Borough Block Lot
identifier.

## Input Rules

- Process one address only.
- If the address is already normalized, call the tool directly.
- If the address is messy but can be normalized confidently, normalize it first.
- If the address cannot be normalized confidently, ask for more information.
- If the address is outside New York City, return an unsupported request.
- Do not invent BBLs.

## Tool

Use only this tool:

- `get_bbl_from_normalized_address`

## Output

Return a concise Markdown answer. Include:

- normalized address parts when useful
- BBL when resolved
- source
- confidence for the normalization judgment
- a clear error when lookup cannot proceed
