#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Run the AQA dashboard against generated data -- no deployment, no cloud, no
# model. One command:
#
#   tools/mock_ui.sh                      # http://localhost:8080
#
# It generates a month of investigations and insights
# (`tools/mock_aqa_data.py`), serves them over the agent's command and A2A
# routes (`tools/mock_aqa_server.py`), and then hands off to `tools/local_ui.sh`, which
# finds the server already listening and just starts the UI against it. That
# hand-off is the point: the dashboard runs through exactly the code path it
# uses with a local agent server, so nothing about it is mock-aware.
#
# The dataset is regenerated on every start, because its history is anchored at
# generation time -- reuse a file and "the last 7 days" slides into the past.
# `MOCK_KEEP=1` reuses an existing one anyway (for a fixed fixture to compare
# renderings against).
#
# Env overrides:
#   MOCK_DATASET  dataset path              (default scratch/mock_aqa.json)
#   MOCK_KEEP     1 = don't regenerate      (default unset)
#   MOCK_DAYS     history depth in days     (default 30)
#   MOCK_RUNS     sweeps per day            (default 4)
#   MOCK_SEED     RNG seed                  (default 7)
#   MOCK_VERIFIED share of sightings with a  (default 0, matching a real
#                 verification result, which  deployment: `verify_clusters`
#                 the dashboard renders as    exists but nothing calls it. Raise
#                 a diagnosis                 it to see the diagnosed half of
#                                             the insight pane at all.)
#   MOCK_ROOT_CAUSE share of sightings with a (default 0, matching a real
#                 recorded root cause, which   deployment: only an RCA chat turn
#                 the dashboard marks on the   writes one. Raise it to see the
#                 list and renders on the      marker, the detail panel and the
#                 detail page                  before/after blocks.)
#   MOCK_SOURCE_DIR a real directory for the  (default unset: a synthesized
#                 Source card to summarize     snapshot. Point it at any repo
#                                              and the card counts that tree.)
#   MOCK_NO_SOURCE 1 = publish no snapshot,  (default unset)
#                 the Source card's amber state
#   MOCK_NO_TELEMETRY 1 = the agent has     (default unset; see below)
#                 exported nothing, so every
#                 sweep fails its first read
#   MOCK_LATENCY  ms between chat frames    (default 120)
#   MOCK_SETTLE   s a started run stays     (default 40; 0 keeps it pending for
#                 pending before finishing   good, which parks ▶ Run disabled)
#   ADK_URL       where the mock listens    (default http://localhost:8000)
#   UI_PORT       dashboard port            (default 8080)
#   The dashboard needs a built bundle -- the image builds one, a checkout
#   does not, and without it `/` says so rather than rendering:
#     npm --prefix ui/web ci && npm --prefix ui/web run build
#
# The three degenerate deployments, for checking that the dashboard -- the Demo
# view especially -- reads as sensible prose when there is nothing to report,
# which is the state a healthy customer is in and a busy dataset never shows:
#
#   MOCK_FAILURE_RATE=0 tools/mock_ui.sh    # nothing fails: no clusters, no insights
#   MOCK_DROP_RATE=1    tools/mock_ui.sh    # nothing survives ingestion
#   MOCK_NO_TELEMETRY=1 tools/mock_ui.sh    # nothing was ever exported
#
# The third is the state a customer meets AQuA in (b/563290003): the agent has
# served no traffic, so every sweep fails its first BigQuery read and the
# dashboard has nothing but failures to draw. It is what the "no data" reading
# of a failed run is checked against.
#
# And the state the pipeline cannot reach yet, because nothing calls the
# verification pass -- the insight pane with a root cause on it:
#
#   MOCK_VERIFIED=0.6   tools/mock_ui.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"

MOCK_DATASET="${MOCK_DATASET:-${REPO_ROOT}/scratch/mock_aqa.json}"
MOCK_DAYS="${MOCK_DAYS:-30}"
MOCK_RUNS="${MOCK_RUNS:-4}"
MOCK_SEED="${MOCK_SEED:-7}"
MOCK_LATENCY="${MOCK_LATENCY:-120}"
MOCK_SETTLE="${MOCK_SETTLE:-40}"
ADK_URL="${ADK_URL:-http://localhost:8000}"
ADK_PORT="${ADK_URL##*:}"; ADK_PORT="${ADK_PORT%%/*}"
ADK_APP="mock_aqa"

[[ -x "$PY" ]] || { echo "ERROR: repo venv not found at $PY (create it first)." >&2; exit 1; }

if curl -sf -o /dev/null --max-time 3 "${ADK_URL}/list-apps"; then
  echo "ERROR: something already listens at ${ADK_URL}. Stop it, or set ADK_URL." >&2
  exit 1
fi

# --- 1. data ---------------------------------------------------------------- #
if [[ -n "${MOCK_KEEP:-}" && -f "$MOCK_DATASET" ]]; then
  echo "Reusing $MOCK_DATASET (MOCK_KEEP set)."
else
  # Unset by default, so the generator's own defaults (the incident shape, a 2%
  # drop rate) decide -- rather than this script re-declaring them and drifting.
  shape=()
  [[ -n "${MOCK_FAILURE_RATE:-}" ]] && shape+=(--failure-rate "$MOCK_FAILURE_RATE")
  [[ -n "${MOCK_DROP_RATE:-}" ]] && shape+=(--drop-rate "$MOCK_DROP_RATE")
  [[ -n "${MOCK_VERIFIED:-}" ]] && shape+=(--verified-rate "$MOCK_VERIFIED")
  [[ -n "${MOCK_ROOT_CAUSE:-}" ]] && shape+=(--root-cause-rate "$MOCK_ROOT_CAUSE")
  [[ -n "${MOCK_SOURCE_DIR:-}" ]] && shape+=(--source-dir "$MOCK_SOURCE_DIR")
  [[ -n "${MOCK_NO_SOURCE:-}" ]] && shape+=(--no-source)
  [[ -n "${MOCK_NO_TELEMETRY:-}" ]] && shape+=(--no-telemetry)
  "$PY" "${REPO_ROOT}/tools/mock_aqa_data.py" \
    --out "$MOCK_DATASET" --days "$MOCK_DAYS" \
    --runs-per-day "$MOCK_RUNS" --seed "$MOCK_SEED" "${shape[@]}"
fi

# --- 2. the mock agent server ----------------------------------------------- #
MOCK_PID=""
cleanup() { [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$PY" "${REPO_ROOT}/tools/mock_aqa_server.py" \
  --dataset "$MOCK_DATASET" --port "$ADK_PORT" --app "$ADK_APP" \
  --latency-ms "$MOCK_LATENCY" --settle-seconds "$MOCK_SETTLE" &
MOCK_PID=$!

echo -n "waiting for the mock server"
for _ in $(seq 1 20); do
  curl -sf -o /dev/null --max-time 2 "${ADK_URL}/list-apps" && { echo " ready."; break; }
  echo -n "."; sleep 0.5
done
curl -sf -o /dev/null --max-time 2 "${ADK_URL}/list-apps" \
  || { echo; echo "ERROR: the mock server did not come up." >&2; exit 1; }

# --- 3. the dashboard, unchanged -------------------------------------------- #
# `local_ui.sh` sees a server already listening at ADK_URL and starts only the
# UI against it -- the same branch a hand-started agent server takes. The
# mock is never standalone, and standalone refuses a running server.
AQA_STANDALONE= ADK_URL="$ADK_URL" ADK_APP="$ADK_APP" UI_PORT="${UI_PORT:-8080}" \
  "${REPO_ROOT}/tools/local_ui.sh"
