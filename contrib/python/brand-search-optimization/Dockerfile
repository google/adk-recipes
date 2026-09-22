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

FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.8.13

RUN useradd --create-home --uid 1000 appuser

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && chown -R appuser:appuser /ms-playwright

WORKDIR /code
RUN chown appuser:appuser /code

COPY --chown=appuser:appuser ./pyproject.toml ./README.md ./uv.lock* ./
COPY --chown=appuser:appuser ./brand_search_optimization ./brand_search_optimization
COPY --chown=appuser:appuser ./.env.example ./.env.example

RUN uv sync --frozen && uv run playwright install --with-deps chromium

RUN chown -R appuser:appuser /code /ms-playwright /home/appuser

USER appuser

ARG AGENT_VERSION=0.1.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "brand_search_optimization.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
