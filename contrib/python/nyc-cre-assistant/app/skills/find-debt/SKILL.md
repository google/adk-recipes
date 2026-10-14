---
name: find-debt
description: Use when the user wants mortgage, lender, debt, or recorded financing evidence for one NYC property.
---

# Find Debt

## Purpose

Given one NYC BBL, find recorded mortgage or debt evidence from verified
public-record tools and return one structured result.

## Input Rules

- Process one property only.
- If the user provides a 10-digit BBL, use it directly.
- If the user provides an address instead of a BBL, first resolve a BBL using
  the address route. Continue only if a BBL is resolved.
- If the input is not a valid 10-digit NYC BBL and cannot be resolved to one,
  return `needs_more_info` or `unsupported`.

## Tool

Use only this tool:

- `find_debt_by_bbl`

## Evidence Rules

- Treat ACRIS metadata as recorded document evidence.
- Treat ACRIS party type `2` as likely lender or mortgagee evidence.
- Treat ACRIS party type `1` as likely borrower or mortgagor evidence.
- Summarize from `recordedDebt`, `currentDebtKnown`, `maturityKnown`,
  `confidence`, and `evidence`.
- Do not infer current outstanding balance from original recorded amount.
- Do not infer payoff status from metadata.
- Do not infer actual maturity date from metadata.

## Stop Rules

- Stop after `find_debt_by_bbl` returns and the final answer is written.
- Do not perform open-ended research.
- Do not call owner tools unless the user asked for ownership.

## Output

Return a concise Markdown summary with status, BBL, primary lender or mortgagee
when available, primary borrower or mortgagor when available, recent relevant
recorded documents, evidence, and a limitation note that current balance,
payoff status, and actual maturity date are not available from ACRIS metadata.
