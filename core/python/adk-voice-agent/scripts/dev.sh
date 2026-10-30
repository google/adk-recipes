#!/bin/sh
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# `kill 0` stops the whole process group on Ctrl-C or either server exiting.
# `--reload-dir app` ignores agent edits under workspaces/.
HOST="${HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
# Vite proxies to the API from this host: loopback reaches a wildcard bind, but a
# specific HOST address is the only one uvicorn listens on.
case "$HOST" in
  0.0.0.0 | :: ) BACKEND_HOST=127.0.0.1 ;;
  *:*) BACKEND_HOST="[$HOST]" ;;
  *) BACKEND_HOST="$HOST" ;;
esac

trap 'trap - INT TERM EXIT; kill 0' INT TERM EXIT
{ VOICE_AGENT_BIND_HOST="$HOST" uv run uvicorn app.fast_api_app:app --host "$HOST" --port "$API_PORT" --reload --reload-dir app; kill 0; } &
{ cd web && VITE_BACKEND_URL="http://$BACKEND_HOST:$API_PORT" npx vite --host "$HOST" --port "$WEB_PORT" --strictPort; kill 0; } &
wait
