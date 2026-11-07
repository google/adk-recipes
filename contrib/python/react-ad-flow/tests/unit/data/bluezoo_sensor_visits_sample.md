# bluezoo_sensor_visits_sample.json — provenance

One-time capture from BlueZoo Morpheus/MO_92 (real venue data, redacted by
construction: only ids/timestamps/counts/valid — the seven named columns of
the live conformer's fixed SELECT; no venue/operator identifiers, no key).

- Sensor selection (owner condition — evidence, not assumption): sensors
  77, 80 chosen as the top valid-row producers over 2026-07-19..2026-07-26
  (counts: 672, 672 — tied with several other sensors at full 96-slot/day
  coverage for all 7 days; 77 and 80 are the first two in the server's
  descending-order result for that tie group). Selection query (Step 0):

  ```sql
  select sensor_id, count(*) as n from sensor_visits
  where timestamp >= '2026-07-19' and timestamp < '2026-07-26' and valid
  group by sensor_id order by n desc
  ```

  MO_92's recent grid coverage is ~34% and 19 live sensors are valid=false,
  so unverified ids fail downstream for data reasons that look like code
  bugs. Full Step 0 result (15 sensors returned, all near-full coverage):
  77, 80, 89, 342, 865, 82, 433, 324, 855, 856, 81, 83, 87, 320 (all n=672),
  343 (n=668).
- Captured: 2026-07-28, via `scripts/bluezoo_probe.py --sql` (read-only,
  client-side SELECT + time-constraint guards)
- Window: 2026-07-25 .. 2026-07-25 (UTC, half-open on the wire as
  `timestamp >= '2026-07-25' and timestamp < '2026-07-26'`); sensors 77, 80;
  no `valid` filter (policy-neutral capture)
- Cost: ~52 KB scanned total (~52 KB payload capture + <1 KB Step 0
  selection query), well under the ~30 KB/2-query budget's intent and far
  under the 500 GB/sensor-location allowance
- Observed real-payload serialization (this is what motivated the task —
  verifying `_parse_utc_naive`/`_coerce_valid` against a genuine payload):
  - `timestamp`: ISO 8601 string with millisecond fraction and an explicit
    UTC offset, e.g. `"2026-07-25T23:45:00.000+00:00"` — NOT the `Z`-suffix
    form the existing unit tests exercised. `_parse_utc_naive` already
    handles this correctly (`datetime.fromisoformat` parses the
    `.000+00:00` suffix natively; the `Z`→`+00:00` replace is a no-op here
    and harmless): confirmed by direct parsing and by the replay test below.
    No code change was needed.
  - `valid`: native JSON boolean (`true`/`false`, i.e. Python `bool` after
    `json.loads`) — every row in this capture is `true` (77 and 80 were
    selected precisely because they have no `valid=false` rows in this
    window). `_coerce_valid` already handles native bools via its
    `isinstance(value, bool)` branch. No code change was needed.
  - counts (`incoming_inner_count`, `outgoing_inner_count`,
    `incoming_outer_count`, `outgoing_outer_count`): native JSON numbers
    (floats), e.g. `9.713000178337097` — not strings. `float(row[...])`
    in `_to_interval` already handles this. No code change was needed.
  - Row shape: exactly the 192 rows expected (2 sensors x 96 15-minute
    slots/day), each with exactly the seven named columns and no others.
- Purpose: every `make test` run parses a genuinely REAL payload shape —
  the owner-approved replacement for the dropped CachedBlueZooAudienceDataSource
  (see 11-live-bluezoo-adapter.md Exit criteria amendment, 2026-07-27).
