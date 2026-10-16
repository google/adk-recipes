"""LiveBlueZooAudienceDataSource — the connected-mode conformer (Phase 11b).

ONE class implementing the seam's ONE method against BlueZoo's REST Data
Warehouse (`run_query`), plus private helpers. Deliberately NOT a framework:
one fixed parameterized SELECT against `sensor_visits`, no pagination, no
query builder, no capability probing (scope guard: the pre-kickoff decisions
at the foot of .docs/version2-plan/11-live-bluezoo-adapter.md).

Conforming rules (findings: bluezoo-live-verification Part 5 +
bluezoo-semantics-resolution):
- Read-only: SELECT-only + mandatory time predicate, both refused
  client-side before anything leaves the process.
- Named columns always — bytes scanned are metered per month; `select *`
  is the expensive mistake.
- Rule R (count only `valid` rows) is config policy (BLUEZOO_VALID_POLICY),
  logged with the row count on every read, never baked in — it is OUR
  recommendation, not yet confirmed by BlueZoo.
- `timestamp` is UTC (proven empirically); rows normalize to NAIVE UTC to
  match the play-slot convention (seed.py's UTC day buckets) — a tz-aware
  datetime would silently match nothing in the attribution join.
- Quota exhaustion is a named, non-retryable operational state; retry makes
  it worse. Bad credentials and wrong-cluster hosts get their own faces too.
- Zero rows is a legitimate result, never an error (fail-closed applies to
  configuration, not to data).
- BLUEZOO_BASE_URL has NO default: the URL is cluster-scoped and travels
  with the AccessKey as a {base_url, access_key} pair — a wrong default
  would fail looking like a bad credential.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

from .. import config
from ..models.attribution import BlueZooVisitInterval
from .datasource import AudienceDataSource

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 60  # probe-proven against MO_92's 5.1M-row tables
_RETRY_BACKOFF_SECONDS = 2.0  # tests monkeypatch to 0

# The one query's column list — named, never *, ~41 bytes/row.
_COLUMNS = (
    "timestamp",
    "sensor_id",
    "incoming_inner_count",
    "outgoing_inner_count",
    "incoming_outer_count",
    "outgoing_outer_count",
    "valid",
)

# BlueZoo rejects any run_query without a constraint on one of these columns
# (undocumented, enforced server-side — probe finding). Belt-and-braces here:
# refusing locally makes "read-only, time-bounded" a property of this module.
_TIME_CONSTRAINT_COLUMNS = ("timestamp", "date_start", "date_end", "date")


class BlueZooError(RuntimeError):
    """A BlueZoo Data Warehouse call failed or returned an unusable response."""


class BlueZooConfigError(BlueZooError):
    """Missing/invalid configuration, rejected credentials, or a wrong
    cluster host — connected mode fails closed with the specific cause."""


class BlueZooQuotaExceeded(BlueZooError):
    """Monthly bytes-scanned allowance exhausted. NON-retryable: every
    run_query fails until it resets or BlueZoo raises it (they do, on
    request — contact BlueZoo). Metadata and the Real-time API keep working."""


class _Retryable(BlueZooError):
    """Internal marker: transient transport failure (timeout / 5xx) worth
    exactly one retry. Never leaves this module."""


def _parse_sensor_map(raw: str) -> dict[int, int]:
    """Parse BLUEZOO_SENSOR_MAP: "101:87,102:433" -> {101: 87, 102: 433}.

    Fail-closed on anything malformed. One-to-one required (duplicate screen
    OR sensor ids rejected) so result rows map back unambiguously.
    """
    mapping: dict[int, int] = {}
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    if not entries:
        raise BlueZooConfigError(
            "BLUEZOO_SENSOR_MAP is empty or unset — connected mode needs at "
            "least one screen_id:sensor_id pair (e.g. '101:87,102:433'). "
            "See SETUP_INSTRUCTIONS.md (connected mode)."
        )
    for entry in entries:
        screen_raw, sep, sensor_raw = entry.partition(":")
        try:
            if not sep:
                raise ValueError
            screen_id, sensor_id = int(screen_raw), int(sensor_raw)
        except ValueError:
            raise BlueZooConfigError(
                f"BLUEZOO_SENSOR_MAP entry {entry!r} is not 'screen_id:sensor_id' "
                "with integer ids (e.g. '101:87')."
            ) from None
        if screen_id in mapping:
            raise BlueZooConfigError(
                f"BLUEZOO_SENSOR_MAP maps screen {screen_id} twice."
            )
        mapping[screen_id] = sensor_id
    if len(set(mapping.values())) != len(mapping):
        raise BlueZooConfigError(
            "BLUEZOO_SENSOR_MAP must be one-to-one: a sensor id appears for "
            "two screens, which would make result rows ambiguous."
        )
    return mapping


def _build_sql(
    sensor_ids: list[int],
    date_from: date,
    date_to: date,
    policy: config.BlueZooValidPolicy,
) -> str:
    """The one fixed SELECT. Half-open UTC window [date_from, date_to + 1 day)
    — `timestamp` is UTC and the interface's dates are inclusive UTC days.
    sensor_ids are ints validated at map parse, so interpolation is safe."""
    end_exclusive = date_to + timedelta(days=1)
    ids = ", ".join(str(s) for s in sorted(sensor_ids))
    sql = (
        f"select {', '.join(_COLUMNS)} from sensor_visits"  # noqa: S608
        f" where timestamp >= '{date_from.isoformat()}'"
        f" and timestamp < '{end_exclusive.isoformat()}'"
        f" and sensor_id in ({ids})"
    )
    if policy is config.BlueZooValidPolicy.VALID_ONLY:
        sql += " and valid"
    return sql


def _guard_sql(sql: str) -> None:
    """Client-side read-only + time-bound refusals, before any network I/O.

    Best-effort (scans the statement, doesn't parse the WHERE clause) — same
    trade-off as the probe: catches the realistic mistake and names the real
    cause instead of surfacing BlueZoo's message after a round trip.
    """
    lowered = sql.lower()
    if not lowered.lstrip().startswith("select"):
        raise BlueZooError(
            f"live conformer issues SELECT statements only: {sql}"
        )
    if not any(column in lowered for column in _TIME_CONSTRAINT_COLUMNS):
        raise BlueZooError(
            "BlueZoo requires every query to constrain one of "
            f"{', '.join(_TIME_CONSTRAINT_COLUMNS)} in its WHERE clause: {sql}"
        )


def _parse_utc_naive(value) -> datetime:
    """Normalize a BlueZoo timestamp to NAIVE UTC.

    The attribution join indexes visits by (screen_id, timestamp) against
    play slots built as naive datetimes (seed.py, UTC-day convention); a
    tz-aware value here would silently match nothing.
    """
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _coerce_valid(value) -> bool:
    """BlueZoo may serialize BOOL as JSON bool or string; bool("false") is
    True, hence this exists."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _classify_http_error(code: int, detail: str) -> BlueZooError:
    """Map a non-retryable HTTP error body to the right exception.

    (5xx never reaches here — the transport wraps those in _Retryable.)
    """
    if "fair use limit" in detail or "data scanned" in detail:
        return BlueZooQuotaExceeded(
            "BlueZoo monthly bytes-scanned allowance exhausted — every "
            "run_query fails until it resets or BlueZoo raises it (they "
            "raise it on request: contact BlueZoo; metadata and the "
            f"Real-time API keep working). Server said: {detail}"
        )
    if "BAD_TOKEN" in detail:
        return BlueZooConfigError(
            "BlueZoo rejected the AccessKey (BAD_TOKEN). Keys are "
            "CLUSTER-scoped: check that BLUEZOO_ACCESS_KEY and "
            "BLUEZOO_BASE_URL are the matching {base_url, access_key} pair "
            f"for this tenant's cluster. Server said: {detail}"
        )
    return BlueZooError(f"run_query: HTTP {code} {detail}")


class LiveBlueZooAudienceDataSource(AudienceDataSource):
    """Real MO_92-shaped audience data through the 11a seam (REST-first)."""

    def __init__(self) -> None:
        self._base_url = (
            os.environ.get("BLUEZOO_BASE_URL", "").strip().rstrip("/")
        )
        self._access_key = os.environ.get("BLUEZOO_ACCESS_KEY", "").strip()
        missing = [
            name
            for name, value in (
                ("BLUEZOO_BASE_URL", self._base_url),
                ("BLUEZOO_ACCESS_KEY", self._access_key),
            )
            if not value
        ]
        if missing:
            raise BlueZooConfigError(
                f"APP_MODE=connected requires BLUEZOO_BASE_URL and "
                f"BLUEZOO_ACCESS_KEY (missing: {', '.join(missing)}). Set them "
                "in app/.env locally, or inject both from Secret Manager at "
                "deploy time — they are a cluster-scoped pair; BLUEZOO_BASE_URL "
                "deliberately has no default. See SETUP_INSTRUCTIONS.md "
                "(connected mode)."
            )
        self._sensor_by_screen = _parse_sensor_map(
            os.environ.get("BLUEZOO_SENSOR_MAP", "")
        )
        self._screen_by_sensor = {
            s: c for c, s in self._sensor_by_screen.items()
        }

    def _call(self, sql: str) -> list[dict]:
        """POST one already-guarded SELECT to run_query; JSON rows back.

        The ONLY method that touches the network — unit tests override it,
        keeping the fast tier network-free by construction.
        """
        request = urllib.request.Request(  # noqa: S310
            f"{self._base_url}/run_query", data=sql.encode()
        )
        request.add_header("Authorization", f"AccessKey {self._access_key}")
        request.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=_TIMEOUT_SECONDS
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code >= 500:
                raise _Retryable(
                    f"run_query: HTTP {exc.code} {detail}"
                ) from exc
            raise _classify_http_error(exc.code, detail) from exc
        except urllib.error.URLError as exc:  # includes socket timeouts
            raise _Retryable(f"run_query: {exc.reason}") from exc
        try:
            rows = json.loads(body)
        except ValueError as exc:
            raise BlueZooConfigError(
                "run_query returned non-JSON — likeliest cause is "
                "BLUEZOO_BASE_URL pointing at the wrong cluster host (a "
                f"proxy or login page answered). Got: {body[:200]!r}"
            ) from exc
        if not isinstance(rows, list):
            raise BlueZooError(
                f"run_query returned an unexpected shape: {rows!r}"
            )
        return rows

    def _query(self, sql: str) -> list[dict]:
        """Guard, send, and retry exactly once on transient failures only.

        Quota, credential, and other 4xx errors are never retried — the
        remedy for each is different and none of them is 'try again'.
        """
        _guard_sql(sql)
        try:
            return self._call(sql)
        except _Retryable as first:
            logger.warning(
                "bluezoo run_query transient failure, retrying once: %s", first
            )
            time.sleep(_RETRY_BACKOFF_SECONDS)
            try:
                return self._call(sql)
            except _Retryable as second:
                raise BlueZooError(
                    f"run_query failed after one retry: {second}"
                ) from second

    def get_visit_intervals(
        self, *, screen_ids: list[int], date_from: date, date_to: date
    ) -> list[BlueZooVisitInterval]:
        # Attribute read at call time so tests (and future config reloads)
        # can monkeypatch the policy — same pattern as the factory's APP_MODE.
        policy = config.BLUEZOO_VALID_POLICY
        sensor_ids = sorted(
            self._sensor_by_screen[s]
            for s in set(screen_ids)
            if s in self._sensor_by_screen
        )
        if not sensor_ids:
            # Interface contract: unknown screens yield no rows, not an error.
            logger.info(
                "bluezoo live read: no mapped sensors for screens=%s — no rows",
                sorted(set(screen_ids)),
            )
            return []
        rows = self._query(_build_sql(sensor_ids, date_from, date_to, policy))
        out = [self._to_interval(row) for row in rows]
        out.sort(key=lambda iv: (iv.screen_id, iv.timestamp))
        # Rule R visibility: policy + row count logged on EVERY live read so
        # the exclusion (or its absence) is auditable per query.
        logger.info(
            "bluezoo live read: policy=%s sensors=%s window=%s..%s rows=%d",
            policy.value,
            sensor_ids,
            date_from.isoformat(),
            date_to.isoformat(),
            len(out),
        )
        return out

    def _to_interval(self, row: dict) -> BlueZooVisitInterval:
        sensor_id = int(row["sensor_id"])
        screen_id = self._screen_by_sensor.get(sensor_id)
        if screen_id is None:
            # The SQL filtered on our sensor ids; a foreign one back is a
            # server-side surprise worth failing loudly on, never dropping.
            raise BlueZooError(
                f"run_query returned unmapped sensor_id={sensor_id}"
            )
        return BlueZooVisitInterval(
            timestamp=_parse_utc_naive(row["timestamp"]),
            screen_id=screen_id,
            # Live rows know nothing of this app's campaigns; occupancy
            # fields stay None (sensor_visits-only query — see module doc).
            incoming_inner_count=float(row["incoming_inner_count"]),
            outgoing_inner_count=float(row["outgoing_inner_count"]),
            incoming_outer_count=float(row["incoming_outer_count"]),
            outgoing_outer_count=float(row["outgoing_outer_count"]),
            valid=_coerce_valid(row["valid"]),
        )
