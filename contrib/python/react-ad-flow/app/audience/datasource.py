"""AudienceDataSource — the audience-measurement seam (Phase 11a).

One interface, two worlds: demo mode's synthetic source (this phase) and
Phase 11b's live BlueZoo conformer answers the same read. The
signature is deliberately BlueZoo-shaped — screens + a date window in,
sensor_visits-shaped intervals out — so nothing synthetic-only (campaign
ids, seed configs) leaks into what a real adapter must implement.

Read-only by design: attribution windows are this app's own domain (the
CMS/ad-play side, written by review_tools' bridge), not audience data.
"""

from abc import ABC, abstractmethod
from datetime import date

from ..models.attribution import BlueZooVisitInterval


class AudienceDataSource(ABC):
    """Read-only source of per-(screen, 15-min slot) visit intervals."""

    @abstractmethod
    def get_visit_intervals(
        self, *, screen_ids: list[int], date_from: date, date_to: date
    ) -> list[BlueZooVisitInterval]:
        """Visit intervals for the given screens, covering the inclusive
        day range [date_from, date_to]. Unknown screens yield no rows."""
