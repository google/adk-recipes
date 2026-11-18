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

# Idempotently seeds /home/agent (updating only shipped files the user has not
# edited) and execs the daemon as `agent`.
set -eu

AGENT=agent
HOME_DIR=/home/agent
SEED=/opt/computer
PY=/opt/venv/bin/python

DROP=""
if [ "$(id -u)" -eq 0 ]; then
  DROP="setpriv --reuid=$AGENT --regid=$AGENT --init-groups"
  mkdir -p "$HOME_DIR"
  if [ "$(stat -c %u "$HOME_DIR")" != "$(id -u "$AGENT")" ]; then
    chown "$AGENT:$AGENT" "$HOME_DIR"
  fi
fi
as_agent() {
  # shellcheck disable=SC2086 # DROP is a word list
  $DROP env HOME="$HOME_DIR" USER="$AGENT" LOGNAME="$AGENT" "$@"
}

as_agent "$PY" - "$SEED" <<'EOF'
import hashlib
import json
import os
import shutil
import sys

home = os.environ["HOME"]
seed = sys.argv[1]

# Track hashes of shipped files so upgrades only overwrite untouched files.
SEEDED = f"{home}/.voice-agent/seeded.json"


def sha256(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_seeded():
    try:
        with open(SEEDED) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


seeded = load_seeded()


def install(src, rel):
    dst = os.path.join(home, rel)
    shipped, current = sha256(src), sha256(dst)
    recorded = seeded.get(rel)
    if not os.path.lexists(dst):
        if recorded:
            return
    elif current is None or (current != shipped and current != recorded):
        return
    if current != shipped:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = f"{dst}.seeding"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    seeded[rel] = shipped


def install_tree(src, rel):
    for root, dirs, files in os.walk(src):
        sub = os.path.normpath(os.path.join(rel, os.path.relpath(root, src)))
        os.makedirs(os.path.join(home, sub), exist_ok=True)
        for name in files:
            install(os.path.join(root, name), os.path.normpath(os.path.join(sub, name)))


install_tree(f"{seed}/home", ".")

store = f"{home}/.agents/skills"
os.makedirs(store, exist_ok=True)
starters = f"{seed}/skills"
for name in sorted(os.listdir(starters)) if os.path.isdir(starters) else []:
    if os.path.isdir(os.path.join(starters, name)):
        install_tree(os.path.join(starters, name), f".agents/skills/{name}")
for link in (f"{home}/.claude/skills", f"{home}/.gemini/config/skills"):
    if not os.path.lexists(link):
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(store, link)

os.makedirs(os.path.dirname(SEEDED), exist_ok=True)
with open(f"{SEEDED}.tmp", "w") as f:
    json.dump(seeded, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(f"{SEEDED}.tmp", SEEDED)


def merge(path, update):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    update(data)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def agy_settings(data):
    # Omit `modelProvider` (`"gemini"` selects API-key mode); runs authenticate via ADC.
    gcp = data.setdefault("gcp", {})
    for key, var in (
        ("project", "GOOGLE_CLOUD_PROJECT"),
        ("location", "GOOGLE_CLOUD_LOCATION"),
    ):
        if os.environ.get(var):
            gcp[key] = os.environ[var]
    # Headless mode aborts on interactive prompts; per-thread agy rules enforce policy denies.
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if "command(*)" not in allow:
        allow.append("command(*)")


# agy MCP config does not expand env vars, so invoke mcp-remote via sh.
GATEWAY = (
    'exec mcp-remote "$VOICE_AGENT_MCP_URL" --transport http-only '
    '--header "Authorization: Bearer $VOICE_AGENT_MCP_TOKEN"'
)


def agy_mcp(data):
    servers = data.setdefault("mcpServers", {})
    servers["voice_agent"] = {"command": "sh", "args": ["-c", GATEWAY]}


merge(f"{home}/.gemini/antigravity-cli/settings.json", agy_settings)
merge(f"{home}/.gemini/config/mcp_config.json", agy_mcp)
EOF

if [ -n "${DOTFILES_REPO:-}" ] && [ ! -e "$HOME_DIR/.dotfiles" ]; then
  as_agent git clone --depth 1 "$DOTFILES_REPO" "$HOME_DIR/.dotfiles" \
    || echo "entrypoint: could not clone DOTFILES_REPO=$DOTFILES_REPO" >&2
fi
if [ -f "$HOME_DIR/.dotfiles/install.sh" ]; then
  (cd "$HOME_DIR/.dotfiles" && as_agent sh ./install.sh) \
    || echo "entrypoint: ~/.dotfiles/install.sh failed" >&2
fi

cd "$HOME_DIR"
if [ "$#" -eq 0 ]; then
  set -- "$PY" -m uvicorn server:app --app-dir "$SEED" --host 0.0.0.0 --port "${PORT:-8080}"
fi
# shellcheck disable=SC2086 # DROP is a word list
exec $DROP env HOME="$HOME_DIR" USER="$AGENT" LOGNAME="$AGENT" "$@"
