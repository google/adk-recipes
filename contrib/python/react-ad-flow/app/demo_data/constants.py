"""The canonical demo-data constants.

DEMO_RPI is THE single revenue-per-impression constant on the demo-data path
(phase-doc step 4). The donor duplicated 0.05 in three places (two class
attributes and a raw SQL literal — mock_inmemory.py:100, mock_bigquery.py:115,
mock_bigquery.py:500, per the ws11a donor audit); here it exists once.
DEMO_RPI is the demo BASE rate: Phase 7 multiplies it by a deterministic
per-(campaign, video) factor in [0.6, 1.4] (see derive.video_rpi), so each
creative has its own stable RPI in [0.03, 0.07]. Still the single canonical
constant — never duplicate it.
"""

# Dollars of attributed revenue per impression (donor's effective constant).
DEMO_RPI = 0.05

# Every generation call fills the backward window [anchor - 29, anchor].
DEMO_WINDOW_DAYS = 30
