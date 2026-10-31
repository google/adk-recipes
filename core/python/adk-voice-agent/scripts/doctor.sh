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

# `make doctor`: checks a user's computer from the inside, read from stdin so it needs
# nothing from the image (`sh -s < scripts/doctor.sh`). Exits 1 when a check fails.
set -u

DAEMON="http://127.0.0.1:${PORT:-8080}"
METADATA="http://metadata.google.internal/computeMetadata/v1"
MIN_FREE_MB="${DOCTOR_MIN_FREE_MB:-2048}"
failed=0

ok() { printf '  ✓ %-14s %s\n' "$1" "$2"; }
bad() { printf '  ✗ %-14s %s\n' "$1" "$2"; failed=1; }

# Version checks only: BROWSER=false keeps any CLI from opening a sign-in page.
echo "Tools"
for tool in claude agy gh git node uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    bad "$tool" "not installed"
    continue
  fi
  version=$(BROWSER=false timeout 20 "$tool" --version 2>&1 </dev/null | head -n 1)
  if [ -n "$version" ]; then
    ok "$tool" "$version"
  else
    bad "$tool" "\`$tool --version\` printed nothing"
  fi
done
if command -v mcp-remote >/dev/null 2>&1; then
  ok "mcp-remote" "installed (agy's bridge client)"
else
  bad "mcp-remote" "not installed"
fi

echo "Daemon"
if curl -fsS -m 5 -o /dev/null "$DAEMON/healthz"; then
  ok "daemon" "$DAEMON/healthz answers"
else
  bad "daemon" "nothing answers at $DAEMON/healthz"
fi

# Harnesses sign in to Vertex AI with Application Default Credentials: the computer's
# service account on GCE. testIamPermissions shows whether it may call models.
echo "Vertex AI"
project="${GOOGLE_CLOUD_PROJECT:-}"
if [ -z "$project" ]; then
  bad "project" "GOOGLE_CLOUD_PROJECT is not set"
else
  ok "project" "$project (location ${GOOGLE_CLOUD_LOCATION:-unset})"
fi
token=""
if account=$(curl -fsS -m 5 -H "Metadata-Flavor: Google" "$METADATA/instance/service-accounts/default/email" 2>/dev/null); then
  token=$(curl -fsS -m 5 -H "Metadata-Flavor: Google" "$METADATA/instance/service-accounts/default/token" 2>/dev/null | jq -r '.access_token // empty')
  if [ -n "$token" ]; then
    ok "credentials" "service account $account"
  else
    bad "credentials" "no access token from the metadata server for $account"
  fi
elif [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  ok "credentials" "GOOGLE_APPLICATION_CREDENTIALS=$GOOGLE_APPLICATION_CREDENTIALS (not checked)"
else
  bad "credentials" "no metadata server and no GOOGLE_APPLICATION_CREDENTIALS"
fi
if [ -n "$token" ] && [ -n "$project" ]; then
  granted=$(curl -fsS -m 10 -X POST \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d '{"permissions": ["aiplatform.endpoints.predict"]}' \
    "https://cloudresourcemanager.googleapis.com/v1/projects/$project:testIamPermissions" 2>&1 \
    | jq -r '.permissions // [] | join(",")' 2>/dev/null)
  if [ "$granted" = "aiplatform.endpoints.predict" ]; then
    ok "vertex" "may call models in $project"
  else
    bad "vertex" "missing aiplatform.endpoints.predict in $project (roles/aiplatform.user)"
  fi
fi

# Without a thread token the bridge answers 401 with its own message, which proves the
# relay, IAP, and the app are all reachable. Anything else stopped on the way.
echo "MCP"
body=$(mktemp)
status=$(curl -sS -m 20 -o "$body" -w '%{http_code}' -X POST \
  -H "Content-Type: application/json" -d '{}' "$DAEMON/mcp-relay/api/v1/mcp" 2>&1)
if [ "$status" = "401" ] && grep -q "Voice Agent MCP token" "$body"; then
  ok "bridge" "the relay reaches the app's MCP bridge"
else
  bad "bridge" "HTTP $status from $DAEMON/mcp-relay: $(head -c 300 "$body")"
fi
rm -f "$body"

echo "Disk"
home="${HOME:-/home/agent}"
free_mb=$(df -Pm "$home" | awk 'NR == 2 {print $4}')
usage=$(df -Ph "$home" | awk 'NR == 2 {print $3 " used of " $2 " (" $5 ")"}')
if [ -n "$free_mb" ] && [ "$free_mb" -ge "$MIN_FREE_MB" ]; then
  ok "home" "$home: $usage"
else
  bad "home" "$home: $usage; under ${MIN_FREE_MB} MB free"
fi

if [ "$failed" -ne 0 ]; then
  echo "Some checks failed; each failed line above says what's wrong."
  exit 1
fi
echo "All checks passed."
