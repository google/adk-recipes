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
# Run the AQA dashboard (ui/) locally, against either agent.
#
# Two modes, chosen by whether an engine is named:
#
#   1. Deployed engine -- set AGENT_ENGINE_RESOURCE_ID (or AGENT_ENGINE_URL) and
#      the UI calls its command and A2A routes. The region comes out of the
#      resource name, so nothing else is needed:
#
#        AGENT_ENGINE_RESOURCE_ID=projects/<num>/locations/<region>/reasoningEngines/<id> \
#          tools/local_ui.sh
#
#   2. Local agent (the default) -- the UI calls the command and A2A routes of
#      a locally served copy of the container's app, for a quick dev loop with
#      no cloud round-trip:
#
#        GOOGLE_CLOUD_PROJECT=<project> tools/local_ui.sh
#
#      If a server is listening at $ADK_URL the UI connects to it.
#      Otherwise this script serves `ambient_quality_agent.fast_api_app` with
#      uvicorn and shuts it down on exit. That app is what the container runs,
#      so it carries the `/investigations/...`, `/insights/...`, `/config` and
#      `/a2a/...` routes the dashboard drives. `adk api_server` serves ADK's own
#      app, which has none of the command routes.
#
# Note that mode 1 runs the *deployed* agent's code: an agent-side change (an
# added command route, say) needs a redeploy before the dashboard can exercise it,
# whereas every UI-side change is picked up from this checkout either way.
#
# Standalone (AQA_STANDALONE=1, what `make standalone` sets) is mode 2 with
# storage in the agent process, not in BigQuery. Two differences:
#
#   - It refuses an engine. A deployed engine has storage of its own.
#   - It refuses a server already listening at $ADK_URL. That server may not be
#     standalone, and reusing it would show its data under a standalone banner.
#
# Env overrides:
#   AGENT_ENGINE_RESOURCE_ID / AGENT_ENGINE_URL   select the deployed engine
#   ADK_URL     agent server base URL          (default http://localhost:8000)
#   ADK_APP     app name the agent server is expected to serve
#               (default ambient_quality_agent)
#   UI_PORT     port for the dashboard         (default 8080)
#   The dashboard is the React app in ui/web; build it first with
#   `npm --prefix ui/web ci && npm --prefix ui/web run build`, or `/` will say
#   so instead of rendering.
#   GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION  needed to auto-start the
#               local agent (model access); irrelevant in engine mode.
#
# The auto-started agent reads its configuration from the environment and from
# src/ambient_quality_agent/.env, with the environment winning.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
UI_DIR="${REPO_ROOT}/ui"

ENGINE_RESOURCE="${AGENT_ENGINE_RESOURCE_ID:-}"
ENGINE_URL="${AGENT_ENGINE_URL:-}"
ADK_URL="${ADK_URL:-http://localhost:8000}"
ADK_APP="${ADK_APP:-ambient_quality_agent}"
UI_PORT="${UI_PORT:-8080}"
ADK_PORT="${ADK_URL##*:}"; ADK_PORT="${ADK_PORT%%/*}"   # port from the URL
case "${AQA_STANDALONE:-}" in
  1|true|TRUE|True) STANDALONE=1 ;;
  *) STANDALONE="" ;;
esac

[[ -x "$PY" ]] || { echo "ERROR: repo venv not found at $PY (create it first)." >&2; exit 1; }
[[ -d "$UI_DIR" ]] || { echo "ERROR: UI not found at $UI_DIR" >&2; exit 1; }

if [[ -n "$STANDALONE" && ( -n "$ENGINE_RESOURCE" || -n "$ENGINE_URL" ) ]]; then
  echo "ERROR: AQA_STANDALONE=1 runs the agent here; unset AGENT_ENGINE_RESOURCE_ID and AGENT_ENGINE_URL." >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Mode 1: a deployed Agent Runtime agent. No local agent to start, so this is just the
# UI with the engine in its environment.
# --------------------------------------------------------------------------- #
if [[ -n "$ENGINE_RESOURCE" || -n "$ENGINE_URL" ]]; then
  echo "Starting AQA dashboard on http://localhost:${UI_PORT}  ->  ${ENGINE_RESOURCE:-$ENGINE_URL}"
  echo "Ctrl-C to stop."
  cd "$UI_DIR"
  exec env \
    AGENT_ENGINE_RESOURCE_ID="$ENGINE_RESOURCE" \
    AGENT_ENGINE_URL="$ENGINE_URL" \
    AQA_BACKEND="${AQA_BACKEND:-auto}" \
    GOOGLE_API_USE_CLIENT_CERTIFICATE="${GOOGLE_API_USE_CLIENT_CERTIFICATE:-false}" \
    PORT="$UI_PORT" \
    "$PY" app.py
fi

# --------------------------------------------------------------------------- #
# Mode 2: a locally-served copy of the container's app.
# --------------------------------------------------------------------------- #
# Pass the agent .env directly to uvicorn to ensure it loads regardless of the
# working directory. The package's `load_dotenv()` preserves values set here.
ENV_FILE="${REPO_ROOT}/src/ambient_quality_agent/.env"
ENV_FILE_ARGS=()
[[ -f "$ENV_FILE" ]] && ENV_FILE_ARGS=(--env-file "$ENV_FILE")
is_set_in_env_file() { [[ -f "$ENV_FILE" ]] && grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE"; }

AGENT_PID=""
cleanup() { [[ -n "$AGENT_PID" ]] && kill "$AGENT_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# --- 1. ensure an agent server is reachable -------------------------------- #
if curl -sf -o /dev/null --max-time 3 "${ADK_URL}/list-apps"; then
  if [[ -n "$STANDALONE" ]]; then
    echo "ERROR: something already listens at ${ADK_URL}. Stop it, or set ADK_URL and UI_PORT." >&2
    exit 1
  fi
  echo "Using agent server already running at ${ADK_URL}"
else
  echo "No agent server at ${ADK_URL}; starting: uvicorn ambient_quality_agent.fast_api_app:app (port ${ADK_PORT})"
  # A default here must not shadow a value in .env: uvicorn loads the file
  # without overriding what the environment already holds.
  for default in GOOGLE_CLOUD_LOCATION=us-central1 GOOGLE_GENAI_USE_VERTEXAI=1 \
                 GOOGLE_API_USE_CLIENT_CERTIFICATE=false; do
    name="${default%%=*}"
    [[ -n "${!name:-}" ]] || is_set_in_env_file "$name" || export "$default"
  done
  # Checks the configuration first, so a missing or invalid setting is reported
  # in one line.
  ( cd "$REPO_ROOT" && uv run python tools/check_config.py ) || exit 1
  ( cd "$REPO_ROOT" && exec uv run uvicorn ambient_quality_agent.fast_api_app:app \
      ${ENV_FILE_ARGS[@]+"${ENV_FILE_ARGS[@]}"} --host 127.0.0.1 --port "$ADK_PORT" ) &
  AGENT_PID=$!
  echo -n "waiting for agent server"
  for _ in $(seq 1 40); do
    curl -sf -o /dev/null --max-time 2 "${ADK_URL}/list-apps" && { echo " ready."; break; }
    echo -n "."; sleep 1
  done
  curl -sf -o /dev/null --max-time 2 "${ADK_URL}/list-apps" \
    || { echo; echo "ERROR: agent server did not come up (see its output above)." >&2; exit 1; }
fi

# A server already listening on ADK_URL may be something other than AQuA's
# agent.
if ! curl -s --max-time 3 "${ADK_URL}/list-apps" | grep -q "\"${ADK_APP}\""; then
  echo "WARNING: the server at ${ADK_URL} does not serve app '${ADK_APP}' (it lists $(curl -s --max-time 3 "${ADK_URL}/list-apps")); is another server on that port?" >&2
fi

# --- 2. run the UI (foreground) -------------------------------------------- #
echo
echo "Starting AQA dashboard on http://localhost:${UI_PORT}  ->  ${ADK_URL}"
if [[ -n "$STANDALONE" ]]; then
  echo "Standalone: storage lives in the agent process and ends with it."
fi
echo "Ctrl-C to stop."
cd "$UI_DIR"
# app.py imports the sibling `ambient_quality_shared` package, which running
# from this directory does not put on the path. In the deployed image the
# staged build context does that instead (see the UI's Dockerfile).
exec env \
  PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  AGENT_ADK_BASE_URL="$ADK_URL" \
  AQA_BACKEND=adk \
  PORT="$UI_PORT" \
  "$PY" app.py
