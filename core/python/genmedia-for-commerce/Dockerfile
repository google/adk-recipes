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

# Multi-stage build: frontend + backend with ADK agent

FROM python:3.13-slim

# Accept build args
ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}
ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

# Install uv, Node.js and system dependencies
RUN pip install --no-cache-dir uv==0.8.13 && \
    apt-get update && apt-get install -y \
    build-essential \
    g++ \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

# Copy dependency files first for better caching
COPY pyproject.toml README.md uv.lock* ./

# Copy application code
COPY genmedia4commerce/ ./genmedia4commerce/
COPY frontend/ ./frontend/
COPY config.env.example* config.env* ./
RUN mkdir -p assets && if [ ! -f config.env ] && [ -f config.env.example ]; then cp config.env.example config.env; fi

# Install Python dependencies from root pyproject.toml
RUN rm -f uv.lock && uv sync --no-cache

# Build frontend
RUN cd /code/frontend && \
    find /code -name ".npmrc" -delete && \
    rm -f ~/.npmrc /root/.npmrc /etc/npmrc && \
    rm -f package-lock.json && \
    npm install --no-audit --no-fund && \
    npm run build

# Pre-download InsightFace model weights (avoids download at runtime)
RUN uv run python -c "\
from insightface.app import FaceAnalysis; \
app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider']); \
app.prepare(ctx_id=-1, det_size=(640, 640)); \
print('InsightFace buffalo_l model ready')"

# Pre-download rembg u2net model (avoids 176MB download at runtime)
RUN uv run python -c "\
from rembg import new_session; \
new_session('u2net'); \
print('rembg u2net model ready')"

# Set PYTHONPATH so imports work (from workflows..., from mcp_server..., etc.)
ENV PYTHONPATH=/code/genmedia4commerce

EXPOSE 8080

# Run the combined FastAPI app (ADK agent + REST + frontend)
CMD ["uv", "run", "uvicorn", "genmedia4commerce.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
