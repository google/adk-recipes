"""Deterministic playout-attribution join (Phase 10).

The causal chain the demo now tells truthfully:

    video_attribution windows (who was live, on which screen, when)
      -> AdPlayRecord schedule (seeded 15-min play slots per video/screen/day)
      -> join vs seed.py's screen_visits (inner counts only -> impressions)
      -> revenue = play visits x video_rpi (the demo stand-in for the PoS
         revenue lookup by product + time window)
      -> daily aggregation -> the same video_metrics rows as before.

Determinism rules (all sha256-seeded via seed._seeded_rng):
- screens_for_campaign: 2-3 screens per campaign's store; screen_id is
  ad_campaign_id * 100 + k, never the campaign id itself (the ws05 1:1
  campaign-as-screen proxy is gone).
- Play schedules are ABSOLUTE per (ad_campaign_id, video_id, screen_id, day):
  activating another creative later never changes existing videos' plays.
  Two creatives may collide on a slot — same deliberate trade-off as ws05's
  fractions summing > 1 (history immutability beats physical exclusivity).
- video_rpi survives from ws07 unchanged: per-(campaign, video), band
  [0.03, 0.07], day- and screen-independent, so each creative's
  revenue/impressions stays one checkable constant.
- Data now arrives via app/audience's AudienceDataSource (demo: seed.py
  behind the seam) — same values, same seeds.

dwell_time_seconds stays a synthetic scalar and circulation stays an
outgoing_outer-derived convention (now summed over played slots) — real
semantics are Phase 11's (app/demo_data/BLUEZOO_MAPPING.md).
"""

from datetime import date, timedelta

from ..models.attribution import AdPlayRecord
from .constants import DEMO_RPI
from .seed import SeedConfig, _seeded_rng, _slot_starts

SLOTS_PER_DAY = 48  # seed.py's grain: 15-min slots, 09:00-21:00
SLOT_MINUTES = 15


def screens_for_campaign(ad_campaign_id: int) -> list[int]:
    """2-3 deterministic screens for the campaign's store.

    IDs are ad_campaign_id * 100 + k — stable, collision-free across
    campaigns, and never equal to the campaign id. Phase 11 maps real
    BlueZoo sensor ids onto this roster."""
    rng = _seeded_rng("screens", ad_campaign_id)
    n = 2 + int(rng.uniform() < 0.5)
    return [ad_campaign_id * 100 + k for k in range(1, n + 1)]


def campaign_uplift(ad_campaign_id: int) -> float:
    """Deterministic per-campaign performance uplift in [0.7, 1.4].

    (Moved from derive.py; unchanged. Phase 15's onboarding relies on any
    campaign id getting a plausible, stable value.)"""
    rng = _seeded_rng("uplift", ad_campaign_id)
    return float(round(0.7 + rng.uniform() * 0.7, 2))


def video_rpi(ad_campaign_id: int, video_id) -> float:
    """Deterministic per-(campaign, video) RPI in [0.03, 0.07].

    (Moved from derive.py; unchanged — ws07 owner decision. DEMO_RPI x a
    seeded factor in [0.6, 1.4], constant across days and screens: each
    creative's revenue/impressions ratio stays ONE checkable constant.)"""
    rng = _seeded_rng("rpi", ad_campaign_id, video_id)
    return float(round(DEMO_RPI * (0.6 + rng.uniform() * 0.8), 4))


def slot_budget(ad_campaign_id: int, video_id, screen_id: int) -> int:
    """Stable number of 15-min slots/day this creative plays on this screen.

    20-45% of the 48 daily slots — the schedule-world successor of ws05's
    video_fraction band. WHICH slots vary per day; how MANY does not."""
    rng = _seeded_rng("slot-budget", ad_campaign_id, video_id, screen_id)
    return round(SLOTS_PER_DAY * (0.20 + rng.uniform() * 0.25))


def _window_covers(window: dict, d: date) -> bool:
    """Day-granularity clamp (windows store datetimes; demo grain is a day)."""
    if window["active_from"].date() > d:
        return False
    active_to = window.get("active_to")
    return active_to is None or d <= active_to.date()


def _plays_for_day(
    ad_campaign_id: int, video_id, screen_id: int, d: date
) -> list[AdPlayRecord]:
    k = slot_budget(ad_campaign_id, video_id, screen_id)
    rng = _seeded_rng(
        "plays", ad_campaign_id, video_id, screen_id, d.isoformat()
    )
    slots = sorted(rng.choice(SLOTS_PER_DAY, size=k, replace=False).tolist())
    starts = _slot_starts(d)
    return [
        AdPlayRecord(
            screen_id=screen_id,
            ad_campaign_id=ad_campaign_id,
            video_id=video_id,
            start=starts[i],
            end=starts[i] + timedelta(minutes=SLOT_MINUTES),
        )
        for i in slots
    ]


def expand_ad_plays(
    ad_campaign_id: int, windows: list[dict], date_from: date, date_to: date
) -> list[AdPlayRecord]:
    """Expand attribution windows into the deterministic play schedule.

    windows: dicts with video_id, screen_id, active_from (datetime),
    active_to (datetime | None; None = still live). Plays are emitted for
    every day in [date_from, date_to] each window covers."""
    plays: list[AdPlayRecord] = []
    for w in windows:
        d = date_from
        while d <= date_to:
            if _window_covers(w, d):
                plays.extend(
                    _plays_for_day(
                        ad_campaign_id, w["video_id"], w["screen_id"], d
                    )
                )
            d += timedelta(days=1)
    return plays


def campaign_seed_config(
    ad_campaign_id: int, date_from: date, date_to: date
) -> SeedConfig:
    """One campaign across its real 2-3 screens (the ws05 1:1
    campaign-as-screen proxy is gone)."""
    return SeedConfig(
        screen_ids=screens_for_campaign(ad_campaign_id),
        campaigns=[
            (
                ad_campaign_id,
                f"campaign-{ad_campaign_id}",
                date_from,
                date_to,
                campaign_uplift(ad_campaign_id),
            )
        ],
        date_from=date_from,
        date_to=date_to,
    )


# Historical import site (tests/unit/test_demo_attribution.py) — keep the
# underscore alias; app/audience/synthetic.py uses the public name.
_campaign_seed_config = campaign_seed_config


def derive_rows_from_windows(
    ad_campaign_id: int,
    video_ids: list,
    windows: list[dict],
    date_from: date,
    date_to: date,
    source=None,  # AudienceDataSource | None — unannotated to avoid the app.audience import edge
) -> list[dict]:
    """The ad-play join: windows -> plays -> visits/revenue -> daily rows.

    impressions = summed incoming_inner_count over the video's played slots
    (inner-only — outer is ~100 m passersby, never impressions).
    revenue = impressions x video_rpi(campaign, video) — the demo stand-in
    for the PoS revenue lookup, coupled to the play schedule so each
    creative's RPI stays its ws07 constant.
    Returned dicts carry exactly the video_metrics insert columns."""
    wanted = set(video_ids)
    if source is None:
        # Function-level import: app/audience imports app/demo_data (the
        # synthetic source wraps seed.py), so the reverse edge must not
        # exist at module import time.
        from ..audience import get_audience_datasource

        source = get_audience_datasource()
    intervals = source.get_visit_intervals(
        screen_ids=screens_for_campaign(ad_campaign_id),
        date_from=date_from,
        date_to=date_to,
    )
    visits_ix = {(iv.screen_id, iv.timestamp): iv for iv in intervals}

    plays = expand_ad_plays(
        ad_campaign_id,
        [w for w in windows if w["video_id"] in wanted],
        date_from,
        date_to,
    )

    # A reactivated video keeps its closed history window alongside a fresh
    # open one (see review_tools._open_attribution_windows), so two windows
    # can cover the same (video, screen) on overlapping days. The play
    # schedule is deterministic per (campaign, video, screen, day), so the
    # windows' overlap produces IDENTICAL AdPlayRecords for the covered
    # slots — dedup by (video_id, screen_id, start) before aggregating so
    # the join never double-counts a slot two windows agree on.
    seen: set = set()
    agg: dict = {}
    for p in plays:
        key = (p.video_id, p.screen_id, p.start)
        if key in seen:
            continue
        seen.add(key)
        v = visits_ix.get((p.screen_id, p.start))
        if v is None:
            continue
        a = agg.setdefault(
            (p.video_id, p.start.date()), {"inner": 0.0, "outer_out": 0.0}
        )
        a["inner"] += v.incoming_inner_count
        a["outer_out"] += v.outgoing_outer_count

    order = {vid: i for i, vid in enumerate(video_ids)}
    rows = []
    for video_id, d in sorted(
        agg, key=lambda key: (key[1], order.get(key[0], 0))
    ):
        a = agg[(video_id, d)]
        impressions = round(a["inner"])
        rpi = video_rpi(ad_campaign_id, video_id)
        dwell_rng = _seeded_rng(
            "dwell-scalar", ad_campaign_id, video_id, d.isoformat()
        )
        rows.append(
            {
                "video_id": video_id,
                "metric_date": d.isoformat(),
                "impressions": impressions,
                # Synthetic seconds-scale scalar — see module docstring.
                "dwell_time_seconds": float(
                    round(4.0 + dwell_rng.uniform() * 8.0, 1)
                ),
                "circulation": round(a["outer_out"]),
                "revenue": round(impressions * rpi, 2),
            }
        )
    return rows
