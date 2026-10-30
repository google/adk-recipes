"""Deterministic, BlueZoo-shaped demo-data generator.

Ported from the donor repo `ad-campaign-agent` @ b6e3302
(`services/audience_provider/seed.py`, branch feat/plan3-slice1-bq-mvp) with
the corrections mandated by the BlueZoo mapping verification
(see app/demo_data/BLUEZOO_MAPPING.md):
HHMM dwell-bin names, `ad_campaign_id` (never bare `campaign_id`),
`minimum_/maximum_visitors_*`, FLOAT64 cuv_freq_*, screen vocabulary, and
frames as lists of dicts (no pandas). Every divergence is enumerated in
app/demo_data/BLUEZOO_MAPPING.md.

Hash-based on (ad_campaign_id, screen_id, date): re-running with the same
config produces byte-identical frames, in any process (sha256-derived seeds,
no PYTHONHASHSEED dependence).

Dates and timestamps are UTC day buckets by convention (Q18 tracks BlueZoo's
own daily-bucket timezone).

Deliberately import-light: stdlib + numpy only, no app modules, no DB —
Phase 11a moved this file behind the AudienceDataSource seam as-is
(app/audience/synthetic.py wraps it; nothing here changed).
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np


def _hhmm(minutes: int) -> str:
    """Encode a dwell-boundary minute count as BlueZoo's HHMM name token."""
    return f"{minutes // 60:02d}{minutes % 60:02d}"


def _bin_columns() -> list[str]:
    """The 106 BlueZoo dwell-histogram bin names, HHMM-encoded.

    Grain layout (verified against the live docs): 30x1min + 15x2min +
    24x5min + 20x15min + 16x60min + 1 open-ended. The donor emitted these in
    minutes encoding (61 of 106 wrong, e.g. `_0060_to_0065`); BlueZoo's real
    names are HHMM (`_0100_to_0105`; boundary bin `_0058_to_0100`).
    """
    edges: list[tuple[int, int]] = []
    for i in range(30):
        edges.append((i, i + 1))
    for i in range(30, 60, 2):
        edges.append((i, i + 2))
    for i in range(60, 180, 5):
        edges.append((i, i + 5))
    for i in range(180, 480, 15):
        edges.append((i, i + 15))
    for i in range(480, 1440, 60):
        edges.append((i, i + 60))
    cols = [f"distribution_bin_{_hhmm(lo)}_to_{_hhmm(hi)}" for lo, hi in edges]
    cols.append("distribution_bin_2400_to_beyond")
    if len(cols) != 106:
        raise ValueError(f"Expected 106 dwell bin columns, got {len(cols)}")
    return cols


DWELL_BIN_COLUMNS = _bin_columns()


@dataclass
class SeedConfig:
    """Configuration for demo data generation.

    campaigns: list of (ad_campaign_id, name, start, end, uplift_factor).
    """

    screen_ids: list[int]
    campaigns: list[tuple[int, str, date, date, float]]
    date_from: date
    date_to: date
    # Two video attributions per campaign (variation A + B)
    videos_per_campaign: int = 2


def _seeded_rng(*keys) -> np.random.Generator:
    """Deterministic per-(key) RNG. Uses sha256 to derive a 64-bit seed."""
    h = hashlib.sha256("|".join(str(k) for k in keys).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "big"))


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _slot_starts(d: date) -> list[datetime]:
    """15-min slots from 09:00 to 21:00 — 48 slots/day (12h x 4)."""
    base = datetime.combine(d, datetime.min.time()).replace(hour=9)
    return [base + timedelta(minutes=15 * i) for i in range(48)]


def _campaign_for_date(cfg: SeedConfig, d: date) -> "tuple[int, float] | None":
    """Return (ad_campaign_id, uplift) for the campaign covering date d, or None."""
    for cid, _name, start, end, uplift in cfg.campaigns:
        if start <= d <= end:
            return cid, uplift
    return None


def _dwell_distribution(rng: np.random.Generator) -> np.ndarray:
    """106-bin dwell histogram. Bimodal: most visitors <2m, smaller bump at 2-10m."""
    weights = np.zeros(106)
    # Bins 0-29 are 1-min bins covering 0-30m
    weights[:30] = rng.dirichlet(np.array([5.0] * 30)) * 0.65
    # Bins 30-44 are 2-min covering 30-60m
    weights[30:45] = rng.dirichlet(np.array([1.0] * 15)) * 0.20
    # Bins 45-68 are 5-min covering 1-3h
    weights[45:69] = rng.dirichlet(np.array([1.0] * 24)) * 0.10
    # Bins 69-88 are 15-min covering 3-8h
    weights[69:89] = rng.dirichlet(np.array([1.0] * 20)) * 0.04
    # Bins 89-104 are 1-h covering 8-24h, plus the 2400_to_beyond
    weights[89:105] = rng.dirichlet(np.array([1.0] * 16)) * 0.009
    weights[105] = 0.001
    return weights / weights.sum()


def _generate_visits(cfg: SeedConfig) -> list[dict]:
    rows = []
    for d in _date_range(cfg.date_from, cfg.date_to):
        match = _campaign_for_date(cfg, d)
        cid, uplift = match if match else (None, 1.0)
        weekend = d.weekday() >= 5
        weekend_mult = 1.25 if weekend else 1.0
        for screen_id in cfg.screen_ids:
            rng = _seeded_rng(cid, screen_id, d.isoformat())
            for slot_idx, ts in enumerate(_slot_starts(d)):
                # Lunch peak (slots 12-16 = 12:00-13:00) and after-school
                hour_factor = 1.0
                if 12 <= slot_idx <= 16:
                    hour_factor = 1.35
                elif 28 <= slot_idx <= 32:
                    hour_factor = 1.15
                base_outer = 200 * uplift * weekend_mult * hour_factor
                base_inner = 40 * uplift * weekend_mult * hour_factor
                rows.append(
                    {
                        "timestamp": ts,
                        "screen_id": screen_id,
                        "ad_campaign_id": cid,
                        "incoming_inner_count": float(
                            round(base_inner * rng.uniform(0.85, 1.15), 2)
                        ),
                        "outgoing_inner_count": float(
                            round(base_inner * rng.uniform(0.80, 1.10), 2)
                        ),
                        "incoming_outer_count": float(
                            round(base_outer * rng.uniform(0.90, 1.10), 2)
                        ),
                        "outgoing_outer_count": float(
                            round(base_outer * rng.uniform(0.85, 1.05), 2)
                        ),
                        "minimum_visitors_inner": float(
                            round(base_inner * 0.05, 2)
                        ),
                        "maximum_visitors_inner": float(
                            round(base_inner * 0.40, 2)
                        ),
                        "average_visitors_inner": float(
                            round(base_inner * 0.20, 2)
                        ),
                        "minimum_visitors_outer": float(
                            round(base_outer * 0.10, 2)
                        ),
                        "maximum_visitors_outer": float(
                            round(base_outer * 0.50, 2)
                        ),
                        "average_visitors_outer": float(
                            round(base_outer * 0.25, 2)
                        ),
                        "valid": True,
                    }
                )
    return rows


def _generate_dwell(visits: list[dict]) -> list[dict]:
    """One row per (timestamp, screen, campaign) matching visits.

    total_visits is inner-only (visits that ended in the slot) — the donor
    summed inner+outer; corrected per the mapping verification.
    """
    rows = []
    for v in visits:
        rng = _seeded_rng(
            "dwell",
            v["ad_campaign_id"],
            v["screen_id"],
            v["timestamp"].isoformat(),
        )
        weights = _dwell_distribution(rng)
        d_row: dict = {
            "timestamp": v["timestamp"],
            "screen_id": v["screen_id"],
            "ad_campaign_id": v["ad_campaign_id"],
            "total_visits": v["incoming_inner_count"],
            "valid": True,
        }
        for col, w in zip(DWELL_BIN_COLUMNS, weights, strict=True):
            d_row[col] = float(w)
        rows.append(d_row)
    return rows


def _generate_uv_daily(cfg: SeedConfig) -> list[dict]:
    rows = []
    for cid, _name, start, end, uplift in cfg.campaigns:
        for d in _date_range(start, end):
            if d < cfg.date_from or d > cfg.date_to:
                continue
            rng = _seeded_rng("uv", cid, d.isoformat())
            total = round(
                800 * uplift * len(cfg.screen_ids) * rng.uniform(0.9, 1.1)
            )
            row: dict = {
                "date": d,
                "ad_campaign_id": cid,
                "unique_visitors_count": total,
            }
            # Heavy long tail: ~75% in freq_1, decreasing.
            # FLOAT64 per BlueZoo (extrapolated from sampled MACs), not INT64.
            shares = np.array(
                [
                    0.75,
                    0.12,
                    0.06,
                    0.03,
                    0.015,
                    0.01,
                    0.008,
                    0.005,
                    0.001,
                    0.001,
                ]
            )
            shares = shares / shares.sum()
            for i, s in enumerate(shares, start=1):
                row[f"cuv_freq_{i}"] = float(round(total * s, 2))
            rows.append(row)
    return rows


def _generate_flow(cfg: SeedConfig) -> list[dict]:
    rows = []
    for cid, _name, start, end, uplift in cfg.campaigns:
        for d in _date_range(start, end):
            if d < cfg.date_from or d > cfg.date_to:
                continue
            for src in cfg.screen_ids:
                for dst in cfg.screen_ids:
                    if src == dst:
                        continue
                    rng = _seeded_rng("flow", cid, d.isoformat(), src, dst)
                    rows.append(
                        {
                            "date": d,
                            "ad_campaign_id": cid,
                            "source_screen_id": src,
                            "dest_screen_id": dst,
                            "transition_count": round(
                                40 * uplift * rng.uniform(0.7, 1.3)
                            ),
                            # Per-pair journey duration is a donor invention
                            # (BlueZoo keys journey duration by
                            # number_of_groups_visited) — see BLUEZOO_MAPPING.md.
                            "average_journey_duration_seconds": float(
                                round(900 * rng.uniform(0.8, 1.2), 1)
                            ),
                        }
                    )
    return rows


def _generate_attribution(cfg: SeedConfig) -> list[dict]:
    rows = []
    for cid, _name, start, end, _u in cfg.campaigns:
        for screen_id in cfg.screen_ids:
            for v_idx in range(cfg.videos_per_campaign):
                video_id = f"vid_c{cid}_s{screen_id}_{v_idx}"
                rows.append(
                    {
                        "video_id": video_id,
                        "ad_campaign_id": cid,
                        "screen_id": screen_id,
                        "active_from": datetime.combine(
                            start, datetime.min.time()
                        ),
                        "active_to": datetime.combine(end, datetime.min.time()),
                    }
                )
    return rows


def generate_frames(cfg: SeedConfig) -> dict[str, list[dict]]:
    """Build the five frames matching the BlueZoo-shaped demo schema."""
    visits = _generate_visits(cfg)
    return {
        "screen_visits": visits,
        "screen_dwell": _generate_dwell(visits),
        "campaign_uv_daily": _generate_uv_daily(cfg),
        "campaign_flow_transition": _generate_flow(cfg),
        "video_attribution": _generate_attribution(cfg),
    }
