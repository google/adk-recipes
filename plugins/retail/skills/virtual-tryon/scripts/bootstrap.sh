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
# Workspace bootstrap for retail-virtual-tryon.
#
# Discovers the skill's install dir, finds a Python 3.11+ interpreter,
# creates .venv in the current directory, installs the skill editable,
# and copies design-spec.md into the workspace.
#
# Run from the workspace directory as a single shell invocation:
#   bash /path/to/scripts/bootstrap.sh

set -e

# 1. Locate the install dir. Try common spec-compliant locations.
SKILL_DIR=""
for candidate in \
    ~/.claude/skills/retail-virtual-tryon \
    ~/.agents/skills/retail-virtual-tryon \
    ~/.gemini/skills/retail-virtual-tryon \
    ~/.cursor/skills/retail-virtual-tryon; do
  if [ -f "$candidate/SKILL.md" ]; then
    SKILL_DIR="$candidate"
    break
  fi
done

# Bootstrap may itself be invoked via absolute path; if so, derive
# SKILL_DIR from $0 as a final fallback.
if [ -z "$SKILL_DIR" ]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  if [ -f "$script_dir/../SKILL.md" ]; then
    SKILL_DIR="$(cd "$script_dir/.." && pwd)"
  fi
fi

if [ -z "$SKILL_DIR" ]; then
  echo "ERROR: retail-virtual-tryon skill not installed. Install it first." >&2
  exit 1
fi

# 2. Pick a Python 3.11+ interpreter.
#    Try PATH lookup first; fall back to absolute paths for sandboxed
#    shells that launch with a stripped PATH.
PYTHON_BIN=""
for py in python3.13 python3.12 python3.11 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    ver=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
    case "$ver" in 3.11|3.12|3.13) PYTHON_BIN="$py"; break ;; esac
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  for path in \
      /opt/homebrew/bin/python3.13 \
      /opt/homebrew/bin/python3.12 \
      /opt/homebrew/bin/python3.11 \
      /usr/local/bin/python3.13 \
      /usr/local/bin/python3.12 \
      /usr/local/bin/python3.11 \
      "$HOME/.pyenv/shims/python3.13" \
      "$HOME/.pyenv/shims/python3.12" \
      "$HOME/.pyenv/shims/python3.11"; do
    if [ -x "$path" ]; then
      ver=$("$path" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
      case "$ver" in 3.11|3.12|3.13) PYTHON_BIN="$path"; break ;; esac
    fi
  done
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: need Python 3.11+. Install one (brew install python@3.12) and retry." >&2
  exit 1
fi

echo "Using PYTHON_BIN=$PYTHON_BIN"
echo "Using SKILL_DIR=$SKILL_DIR"

# 3. Create venv, activate, install the skill editable.
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
pip install -e "${SKILL_DIR}"

# 4. Copy the design-spec template into the workspace.
if [ ! -f ./design-spec.md ]; then
  cp "$SKILL_DIR/assets/design-spec.md" ./design-spec.md
fi

echo ""
echo "READY"
echo "  SKILL_DIR=$SKILL_DIR"
echo "  workspace=$(pwd)"
echo "  venv=$(pwd)/.venv"
echo "  design-spec=$(pwd)/design-spec.md"
