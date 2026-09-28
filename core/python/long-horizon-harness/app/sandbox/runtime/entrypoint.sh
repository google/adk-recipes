#!/bin/sh
set -e

exec python -m uvicorn server:app --app-dir /opt/runtime --host 0.0.0.0 --port 8080
