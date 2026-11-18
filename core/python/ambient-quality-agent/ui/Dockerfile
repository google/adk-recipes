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

# --- the front end ---------------------------------------------------------- #
#
# This stage is not an optimisation, it is the only thing that puts the bundle
# in the image. `static_v2/` is gitignored and excluded from the build context,
# so without a build here the image would ship whatever happened to be lying on
# the machine that built it -- which is nothing, in CI and in Cloud Build. The
# front end's own tests would keep passing all the while.
#
# The Node major matches web/.nvmrc, the version frontend-tests.yml type-checks
# with: the version that builds the code should be the version it was checked
# against. tools/ci/check_conventions.py enforces this.
FROM node:26.12.1-slim@sha256:94816674c62774da0d099a947bf4613b9e1972a2733abd0d0cd80e795f8411d0 AS web-builder

WORKDIR /build

# The manifests alone first, so the dependency layer survives a change to the
# application source -- `npm ci` is ~30s, the build itself ~2s.
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci

COPY web/ ./web/
# vite.config.ts sets `outDir: "../static_v2"`, so this writes /build/static_v2.
RUN cd web && npm run build

# --- the service -------------------------------------------------------------- #
# FastAPI dashboard — single, compact Cloud Run image.
FROM python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

RUN printf '%s\n' \
      'uv==0.13.6 \' \
      '    --hash=sha256:22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba \' \
      '    --hash=sha256:7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b \' \
      '    --hash=sha256:2e9a99100abe4f99605366974c4f9fe27f236615e05de8e78c0f357e38f09f97 \' \
      '    --hash=sha256:d762215478854f6cf20c3faed8f1eb30c8101d568e7bc32a3abafc063929d5c5' \
      > /tmp/uv-requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/uv-requirements.txt \
 && rm /tmp/uv-requirements.txt

# Serve as an unprivileged user, so a compromised server does not hold root
# inside the container.
#
# The user is created before the dependency install so that /app and the venv it
# builds are owned by that user from the start.
#
# UID/GID are explicit because image-assigned IDs are non-deterministic across
# rebuilds; --no-log-init avoids the Go archive/tar sparse-file blowup that can
# exhaust disk when creating users.
RUN groupadd --system --gid 1001 dashboard \
 && useradd --system --uid 1001 --gid 1001 --no-log-init \
      --create-home --home-dir /home/dashboard dashboard \
 && mkdir -p /app \
 && chown dashboard:dashboard /app

# USER does not update HOME, which would leave anything that resolves `~`
# pointing at the root-owned /root.
ENV HOME=/home/dashboard \
    UV_CACHE_DIR=/home/dashboard/.cache/uv

USER 1001:1001

WORKDIR /app

COPY --chown=dashboard:dashboard ./pyproject.toml ./uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    UV_LOCKED=1 \
    UV_NO_SYNC=1

# COPY writes as UID 0 regardless of USER, and keeps the file modes of the build
# context, which follow the deploying user's umask. Under a umask such as 027
# root-owned files would be unreadable to the server, so they are handed to it.
COPY --chown=dashboard:dashboard . .

# The build context combines this directory and the sibling `ambient_quality_shared`
# package via `extension/acli_ui.py` or `ui/deploy_ui_cloud_run.sh`. Building directly
# without staging causes import failures at runtime; these assertions fail fast
# during the image build if required files are missing.
#
# Verifies app.py as well, ensuring missing entrypoints are caught during the build
# rather than as generic runtime ASGI load failures.
RUN test -f app.py || { \
      echo "app.py is missing from the build context." >&2; \
      echo "Deploy with agents-cli deploy, or ui/deploy_ui_cloud_run.sh." >&2; \
      exit 1; }
RUN test -d ambient_quality_shared || { \
      echo "ambient_quality_shared/ is missing from the build context." >&2; \
      echo "Deploy with agents-cli deploy, or ui/deploy_ui_cloud_run.sh." >&2; \
      exit 1; }

# Beside app.py, which is where it looks for it. It is the only thing `/`
# serves, so an image missing this layer answers 503 there. See the module
# docstring in app.py.
COPY --chown=dashboard:dashboard --from=web-builder /build/static_v2 ./static_v2

EXPOSE 8080

# uvicorn honors $PORT (Cloud Run injects it). One worker keeps the in-memory
# per-conversation client map coherent; scale out with a shared session store.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
