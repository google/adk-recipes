#!/usr/bin/env bash
set -euo pipefail

recipe_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$recipe_dir"

exec uv run uvicorn app.fast_api_app:app \
  --host 127.0.0.1 \
  --port "${PORT:-8080}"
