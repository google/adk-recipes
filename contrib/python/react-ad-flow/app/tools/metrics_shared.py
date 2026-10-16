"""Shared metric computation — the ONLY place RPI math lives.

docs/METRICS.md is the authoritative definition source; this module is its
executable counterpart (Phase 4 / 04-centralize-rpi-metrics). Every tool
that returns an RPI value calls compute_rpi() instead of dividing inline;
python-level cross-row averaging of already-averaged quantities (dwell
time) goes through compute_weighted_average().

Both functions are pure arithmetic: no queries, no scoping logic. Callers
sum revenue/impressions over their own window first.
"""


def compute_rpi(total_revenue: float, total_impressions: float) -> float:
    """Revenue per impression: ratio of sums, never sum of ratios.

    Returns 0.0 when impressions are zero, negative, or None (convention
    documented in docs/METRICS.md — every pre-centralization call site
    already returned 0 for that case). None revenue is treated as 0.
    """
    if not total_impressions or total_impressions <= 0:
        return 0.0
    return round((total_revenue or 0) / total_impressions, 4)


def compute_weighted_average(
    rows: list, value_field: str, weight_field: str
) -> float:
    """Average of rows[value_field] weighted by rows[weight_field].

    For aggregating already-averaged quantities (e.g. daily dwell-time
    averages) across rows of unequal size — an unweighted mean of averages
    over-weights small rows. Missing/None fields count as 0; returns 0.0
    when total weight is zero.
    """
    total_weight = sum((row.get(weight_field) or 0) for row in rows)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(
        (row.get(value_field) or 0) * (row.get(weight_field) or 0)
        for row in rows
    )
    return round(weighted_sum / total_weight, 4)
