"""SyntheticAudienceDataSource — seed.py behind the seam (Phase 11a).

Wraps the Phase 5 deterministic generator as-is: frames are generated with
the same campaign_seed_config the join built directly before this phase
(full campaign screen set, then filtered), so values are byte-identical.
Campaign context derives from the ws10 screen-id convention
(screen_id = ad_campaign_id * 100 + k) — the interface itself stays free
of synthetic-only parameters.
"""

from datetime import date

from ..demo_data.attribution import campaign_seed_config
from ..demo_data.seed import generate_frames
from ..models.attribution import BlueZooVisitInterval
from .datasource import AudienceDataSource


class SyntheticAudienceDataSource(AudienceDataSource):
    """Deterministic demo audience data, per-(screen, 15-min slot)."""

    def get_visit_intervals(
        self, *, screen_ids: list[int], date_from: date, date_to: date
    ) -> list[BlueZooVisitInterval]:
        wanted = set(screen_ids)
        out: list[BlueZooVisitInterval] = []
        for cid in sorted({sid // 100 for sid in wanted}):
            frames = generate_frames(
                campaign_seed_config(cid, date_from, date_to)
            )
            out.extend(
                BlueZooVisitInterval(**row)
                for row in frames["screen_visits"]
                if row["screen_id"] in wanted
            )
        out.sort(key=lambda iv: (iv.screen_id, iv.timestamp))
        return out
