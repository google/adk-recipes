---
name: find-owner
description: Use when the user wants owner evidence for one NYC property from a BBL or address.
---

# Find Owner

## Purpose

Given one NYC BBL, find likely owner evidence from verified public-record tools
and return one concise result.

## Input Rules

- Process one property only.
- If the user provides a 10-digit BBL, use it directly.
- If the user provides an address instead of a BBL, first resolve a BBL using
  the address route. Continue only if a BBL is resolved.
- If the input is not a valid 10-digit NYC BBL and cannot be resolved to one,
  return `needs_more_info` or `unsupported`.

## Tool

Use only this tool:

- `find_owner_by_bbl`

## Evidence Rules

- Treat PLUTO `ownerName` as tax-lot owner evidence.
- Treat ACRIS parties as deed or mortgage party evidence, not automatic proof
  of economic ownership.
- Treat NYS DOS data as entity metadata and service-of-process evidence, not
  proof of human ownership.
- Summarize from `ownerEntity`, `connectedPeople`, `confidence`, and
  `evidence`.
- Do not infer hidden human owners.
- Do not claim a person is a decision-maker unless a verified tool result
  directly supports that claim.

## Stop Rules

- Stop after `find_owner_by_bbl` returns and the final answer is written.
- Do not perform open-ended research.

## Output

Return a concise Markdown summary with status, BBL, owner entity, connected
people if verified, confidence, supporting evidence, and a limitation note that
entity metadata is evidence, not proof of hidden human ownership.
