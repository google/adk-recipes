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

# Lightweight Cloud Run Container for ADK Token Control Tower
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /app

COPY ./pyproject.toml ./README.md ./uv.lock* ./
COPY ./adk_tokenomics_plugin ./adk_tokenomics_plugin
COPY ./.env.example ./.env

RUN uv sync --frozen --no-dev --extra bigquery \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /home/appuser

USER appuser

EXPOSE 8080

# Allow mapped loopback host ports (e.g. docker run -p <host>:8080) by default;
# pass TOKENOMICS_ALLOWED_HOSTS=*.run.app for Cloud Run or external HTTPS proxies.
CMD ["sh", "-c", "exec uv run --no-sync adk-tokenomics web --host 0.0.0.0 --port ${PORT:-8080} --allowed-host 127.0.0.1 --allowed-host localhost --no-browser"]
