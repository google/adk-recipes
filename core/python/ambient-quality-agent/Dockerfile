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

# Container image for the AQA orchestrator on Agent Runtime.
#
# Self-contained: `docker build .` from a checkout of this repository produces
# a working image, and Agent Runtime builds the same thing remotely from
# uploaded source.
#
# Agent Runtime injects no serving code into a custom container -- it builds
# this Dockerfile and runs its CMD, and the container is required to speak the
# runtime contract itself (listen on 0.0.0.0:8080; serve /api/reasoning_engine
# and /api/stream_reasoning_engine). That is why the CMD below points at an
# ASGI app we ship rather than at the agent module directly. See:
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract

FROM python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

RUN printf '%s\n' \
      'uv==0.13.6 \' \
      '    --hash=sha256:22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba \' \
      '    --hash=sha256:7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b \' \
      '    --hash=sha256:2e9a99100abe4f99605366974c4f9fe27f236615e05de8e78c0f357e38f09f97 \' \
      '    --hash=sha256:d762215478854f6cf20c3faed8f1eb30c8101d568e7bc32a3abafc063929d5c5' \
      > /tmp/uv-requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/uv-requirements.txt \
 && rm /tmp/uv-requirements.txt

# Serve as an unprivileged user instead of UID 0.
#
# The user is created *before* the dependency install so that /code and the
# venv it builds are owned by that user from the start.
#
# UID/GID are explicit because image-assigned IDs are non-deterministic across
# rebuilds; --no-log-init avoids the Go archive/tar sparse-file blowup that can
# exhaust disk when creating users.
RUN groupadd --system --gid 1001 agent \
 && useradd --system --uid 1001 --gid 1001 --no-log-init \
      --create-home --home-dir /home/agent agent \
 && mkdir -p /code \
 && chown agent:agent /code

# USER does not update HOME, which would leave uv writing its cache to the
# root-owned /root.
ENV HOME=/home/agent \
    UV_CACHE_DIR=/home/agent/.cache/uv

USER 1001:1001

WORKDIR /code

# COPY writes as UID 0 regardless of USER, so ownership has to be spelled out.
COPY --chown=agent:agent ./pyproject.toml ./README.md ./uv.lock ./

# Named explicitly rather than copying all of src/, which also holds the
# standalone chat UI -- that has its own deployment and no place in this image.
# `ambient_quality_shared` is here for `protocol`, which defines the command
# route paths this container serves and its front-ends call. `aqua_atif` holds
# the ATIF trajectory models and the rules for reading ATIF files.
COPY --chown=agent:agent ./src/ambient_quality_agent ./src/ambient_quality_agent
COPY --chown=agent:agent ./src/ambient_quality_shared ./src/ambient_quality_shared
COPY --chown=agent:agent ./src/aqua_atif ./src/aqua_atif

# Dependencies only. Installing the project itself would make hatch build the
# wheel described by [tool.hatch.build.targets.wheel], which is unnecessary
# here. --no-dev leaves pytest and cloudpickle out: they are reachable only
# through the dev group, and nothing under src/ imports them.
RUN uv sync --locked --no-install-project --no-dev

# `ambient_quality_agent.…` absolute imports (~130 of them) resolve against the
# package's *parent*, so src/ is what goes on the path, not the package.
ENV PYTHONPATH=/code/src

# Keep the `uv run` in CMD from re-resolving or mutating the venv that was
# already built above. Set after that sync so they cannot influence it.
ENV UV_LOCKED=1 \
    UV_NO_SYNC=1

# Enables GenAI message content capture on exported spans for container
# environments (e.g., Cloud Run, local Docker). On Agent Runtime,
# deploymentSpec.env overrides container ENV, so extension/acli_deploy.py passes
# these via --update-env-vars.
ENV OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "ambient_quality_agent.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
