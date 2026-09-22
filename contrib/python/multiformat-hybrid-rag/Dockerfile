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

FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.8.13

# Run as an unprivileged user. Switched before `uv sync` so the virtualenv
# and uv's cache are owned by the runtime user: `uv run` re-validates the
# environment at container start and needs write access to both.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /code
RUN chown appuser:appuser /code
USER appuser

COPY --chown=appuser:appuser ./pyproject.toml ./README.md ./uv.lock* ./

COPY --chown=appuser:appuser ./app ./app
COPY --chown=appuser:appuser ./src ./src
# .env.example, not .env: .env is gitignored and absent from the Cloud Build
# context. It supplies non-secret defaults; real values arrive as Cloud Run
# environment variables, which win because every loader uses override=False.
COPY --chown=appuser:appuser ./.env.example ./.env.example

RUN uv sync --frozen

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]