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

# Three stages: frontend build, Python deps build, slim runtime.
# The product catalogue (assets/catalog/products.parquet) is copied in; no
# config.env is baked in: config comes from env vars. The app runs as an
# unprivileged user (uid 10001). The base images are floating tags on
# purpose: each deploy builds on the latest patched base.

# ---- Frontend ---------------------------------------------------------------
FROM node:20-slim AS frontend

ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

WORKDIR /code/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Python dependencies ----------------------------------------------------
FROM python:3.12-slim AS python-build

RUN pip install --no-cache-dir uv==0.10.9

ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /code
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# ---- Runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home \
    --shell /usr/sbin/nologin app

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

WORKDIR /code
COPY --from=python-build /code/.venv /code/.venv
COPY --from=frontend /code/frontend/dist /code/frontend/dist
COPY pyproject.toml ./
COPY genmedia4commerce/ ./genmedia4commerce/
COPY assets/catalog/products.parquet ./assets/catalog/products.parquet

ENV PATH=/code/.venv/bin:$PATH
EXPOSE 8080
USER 10001

CMD ["uvicorn", "genmedia4commerce.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
