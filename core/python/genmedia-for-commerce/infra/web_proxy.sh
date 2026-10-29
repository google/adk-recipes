#!/bin/bash
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

# Open the deployed web app through `gcloud run services proxy`, at
# http://localhost:8080. The Cloud Run service requires IAM authentication,
# so a browser cannot open its URL; the proxy adds an identity token to
# every request. In Cloud Shell, Web Preview on port 8080 opens it
# (config.env's ALLOW_ORIGINS lists that origin and localhost:8080's).
#
# The token is the genmedia-web account's (make setup-infra creates it and
# gives the deployer Token Creator on it, make deploy lets it invoke the
# service, make share lets teammates act as it), minted for the service URL,
# the proxy's target: Cloud Shell's own credentials cannot mint one. When
# that fails, the caller's own token (user credentials on a laptop). A token
# lasts an hour, so the proxy restarts with a fresh one every 50 minutes.
#
#   infra/web_proxy.sh               the proxy in the foreground (make web)
#   infra/web_proxy.sh --hint        how to open the web app
#   infra/web_proxy.sh --background  the proxy in the background when stdin
#       and stdout are a terminal (not CI) and the port is free, otherwise
#       the hint (make deploy, make register-gemini-enterprise). It stops with the
#       terminal session (the session leader, polled every second) or
#       with `kill <pid>`, which stop the proxy and its children; its output goes to a temporary log file.
#
# PROJECT_ID, DEFAULT_REGION and SERVICE_NAME come from the environment (the
# Makefile reads them from config.env). For the tests only: WEB_PROXY_PORT
# changes the port (ALLOW_ORIGINS lists the origins of port 8080),
# WEB_PROXY_REFRESH the seconds between token refreshes, WEB_PROXY_RETRY
# the seconds before a failed refresh is tried again and WEB_PROXY_POLL the
# seconds between checks of the proxy and the session.

set -euo pipefail

PORT="${WEB_PROXY_PORT:-8080}"
REFRESH="${WEB_PROXY_REFRESH:-3000}"
RETRY="${WEB_PROXY_RETRY:-60}"
POLL="${WEB_PROXY_POLL:-1}"
URL="http://localhost:$PORT"
SERVICE="${SERVICE_NAME:-genmedia-for-commerce}"
REGION="${DEFAULT_REGION:-us-central1}"
PROJECT="${PROJECT_ID:-}"
if [ -z "$PROJECT" ]; then
    echo "Error: PROJECT_ID is not set in config.env" >&2
    exit 1
fi
WEB_SA="genmedia-web@$PROJECT.iam.gserviceaccount.com"
COMMAND="gcloud run services proxy $SERVICE --region $REGION --project $PROJECT --port $PORT"
# --quiet: no prompt, as the component is installed first (ensure_component)
PROXY=(gcloud run services proxy "$SERVICE" --region "$REGION"
    --project "$PROJECT" --port "$PORT" --quiet)
LOCATION=(--region "$REGION" --project "$PROJECT")
PREVIEW="Web Preview > Preview on port $PORT"
ACCESS="Only people with access to the project, or added with make share, can open it."

hint() {
    echo "Open the web app: make web (or: $COMMAND)"
    echo "then $URL (in Cloud Shell: $PREVIEW). $ACCESS"
    echo "Right after a deploy, a 403 means the access grant is still applying: wait a minute and reload."
}

port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

has_proxy_binary() {
    local root
    command -v cloud-run-proxy >/dev/null 2>&1 && return 0
    root=$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null) ||
        return 1
    [ -n "$root" ] && [ -x "$root/bin/cloud-run-proxy" ]
}

# gcloud run services proxy runs the cloud-run-proxy component, and asks to
# install it when it is missing. A gcloud installed with apt (Cloud Shell)
# has no component manager, so the prompt's install fails: install it here.
ensure_component() {
    local root
    has_proxy_binary && return 0
    echo "Installing cloud-run-proxy, the gcloud component that gcloud run services proxy runs..."
    root=$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null) ||
        root=""
    case "$root" in
    /usr/*)
        if command -v apt-get >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
            sudo -n apt-get install -y -qq google-cloud-cli-cloud-run-proxy ||
                { sudo -n apt-get update -qq &&
                    sudo -n apt-get install -y -qq google-cloud-cli-cloud-run-proxy; } ||
                true
        fi
        ;;
    *) gcloud components install cloud-run-proxy --quiet || true ;;
    esac
    has_proxy_binary && return 0
    echo "Could not install it. Run gcloud components install cloud-run-proxy (for a gcloud installed with apt: sudo apt-get install google-cloud-cli-cloud-run-proxy), then try again." >&2
    return 1
}

# The session id: the pid of the session leader, the terminal's shell
session_id() {
    local sid
    sid=$(ps -o sid= -p $$ 2>/dev/null | tr -d ' ') || sid=""
    if [ -z "$sid" ]; then # macOS ps has no sid
        sid=$(python3 -c 'import os; print(os.getsid(0))' 2>/dev/null) ||
            sid=""
    fi
    echo "$sid"
}

# Stop a process and its descendants (gcloud runs the cloud-run-proxy binary)
kill_tree() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        kill_tree "$child"
    done
    kill "$1" 2>/dev/null || true
}

# The service URL, which the proxy targets (status.url): the token's audience
service_url() {
    gcloud run services describe "$SERVICE" "${LOCATION[@]}" \
        --format='value(status.url)'
}

# The identity token of the genmedia-web account for the service URL
web_sa_token() {
    local url
    url=$(service_url) && [ -n "$url" ] || return 1
    gcloud auth print-identity-token --impersonate-service-account="$WEB_SA" \
        --audiences="$url"
}

# A token for the proxy: genmedia-web's, else the caller's own (user
# credentials can mint one, Cloud Shell's cannot); gcloud's errors are
# shown only when both fail.
fetch_token() {
    local errors token
    errors=$(mktemp "${TMPDIR:-/tmp}/genmedia-web-token.XXXXXX")
    if token=$(web_sa_token 2>"$errors") && [ -n "$token" ]; then
        rm -f "$errors"
        echo "$token"
        return 0
    fi
    if token=$(gcloud auth print-identity-token 2>>"$errors") &&
        [ -n "$token" ]; then
        echo "Could not act as $WEB_SA: the proxy uses your own identity token." >&2
        rm -f "$errors"
        echo "$token"
        return 0
    fi
    tail -n 5 "$errors" >&2
    rm -f "$errors"
    echo "Error: no identity token for the proxy. Ask the project's deployer to run make share USERS=user:<you> (roles/iam.serviceAccountTokenCreator on $WEB_SA, and make setup-infra then make deploy if that account is missing), or on a laptop sign in with gcloud auth login." >&2
    return 1
}

# Wait (at most 10 s) for the port to be free after the proxy stopped
wait_port_free() {
    local tries=0
    while port_in_use && [ "$tries" -lt 50 ]; do
        sleep 0.2
        tries=$((tries + 1))
    done
}

# Start the proxy with a token in the background: its pid in $proxy
start_proxy() {
    "${PROXY[@]}" --token="$1" </dev/null &
    proxy=$!
}

# Restart the proxy with a fresh token; when no token comes, keep the
# running one and try again RETRY seconds later. Sets $proxy and $next.
refresh_proxy() {
    local token
    if ! token=$(fetch_token); then
        next=$((SECONDS + RETRY))
        return 0
    fi
    echo "Restarting the proxy with a fresh identity token (a token lasts an hour)."
    kill_tree "$proxy"
    wait "$proxy" 2>/dev/null
    wait_port_free
    start_proxy "$token"
    next=$((SECONDS + REFRESH))
}

session_ended() {
    [ -n "$1" ] && ! ps -p "$1" >/dev/null 2>&1
}

# Run the proxy until it exits, refreshing its token every REFRESH seconds;
# with a session id, stop it when that terminal session ends. A signal
# (kill <pid>, Ctrl+C, a closed terminal) stops it and its children.
serve() {
    local session=$1 proxy="" nap="" next token
    set +e
    trap 'echo "Stopped by a signal."; kill_tree "$proxy"; [ -z "$nap" ] || kill "$nap" 2>/dev/null; exit 0' TERM HUP INT
    token=$(fetch_token) || return 1
    start_proxy "$token"
    next=$((SECONDS + REFRESH))
    while kill -0 "$proxy" 2>/dev/null; do
        if session_ended "$session"; then
            echo "The terminal session ended: proxy stopped."
            kill_tree "$proxy"
            return 0
        fi
        [ "$SECONDS" -lt "$next" ] || refresh_proxy
        sleep "$POLL" &
        nap=$!
        wait "$nap"
    done
    wait "$proxy"
}

# Run in the background: the proxy, stopped when the terminal session ends.
# A child started with & outlives a closed terminal (the shell sends SIGHUP
# to its own jobs only, and make's job has ended).
watch_proxy() {
    serve "$1" >"$2" 2>&1
}

background() {
    local log pid waited=0
    if ! [ -t 0 ] || ! [ -t 1 ] || [ -n "${CI:-}" ]; then
        hint
        return 0
    fi
    if port_in_use; then
        echo "Port $PORT is in use (the proxy started earlier?), so the web app proxy was not started."
        hint
        return 0
    fi
    if ! ensure_component; then
        hint
        return 0
    fi
    log=$(mktemp "${TMPDIR:-/tmp}/genmedia-web-proxy.XXXXXX")
    watch_proxy "$(session_id)" "$log" </dev/null >/dev/null 2>&1 &
    pid=$!
    echo "Starting the web app proxy ($COMMAND)..."
    while ! port_in_use; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "The proxy stopped:"
            tail -n 20 "$log"
            hint
            return 0
        fi
        if [ "$waited" -ge 60 ]; then
            echo "The proxy has not answered on port $PORT after 60 s: see $log"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo
    echo "The web app is running in the background at $URL (Cloud Shell: Web Preview > port $PORT); stop it with: kill $pid"
    echo "It stops when this terminal session ends. $ACCESS Log: $log"
}

foreground() {
    echo "Web app: $URL (in Cloud Shell: $PREVIEW). Ctrl+C stops it. $ACCESS"
    if port_in_use; then
        echo "Error: port $PORT is in use (the proxy already running? Then open $URL)." >&2
        exit 1
    fi
    ensure_component
    serve ""
}

case "${1:-}" in
"") foreground ;;
--hint) hint ;;
--background) background ;;
*)
    echo "Usage: $0 [--hint | --background]" >&2
    exit 2
    ;;
esac
