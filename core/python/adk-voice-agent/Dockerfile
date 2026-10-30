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

# One container serves both the API and the web UI (FastAPI serves web/dist at `/`).

# --- Web UI build ---
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- API + built UI ---
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

# git: the server seeds the workspace repositories on startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 voice_agent

WORKDIR /code

# Dependencies first so code edits reuse this layer. No dev group: tests and
# agents-cli don't ship.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
# The built-in Google Workspace MCP servers the gateway spawns (`python -m workspace_mcp.<x>`).
COPY workspace_mcp ./workspace_mcp
COPY config ./config
# The connected apps' MCP servers, which the MCP gateway proxies (app/api/mcp_gateway.py).
COPY .mcp.json ./
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist ./web/dist

# Code stays root-owned and read-only; the app may create workspaces/ here, and
# keeps its thread store in ~/.adk-voice-agent.
RUN chown voice_agent:voice_agent /code
USER voice_agent

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PATH="/code/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Run from the venv rather than `uv run`, which would try to sync the dev group at startup.
CMD ["sh", "-c", "exec uvicorn app.fast_api_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
