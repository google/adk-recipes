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
# node:22 to match web/.nvmrc and the version frontend-tests.yml type-checks
# with: the version that builds the code should be the version it was checked
# against.
FROM node:22-slim AS web-builder

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
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

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
COPY --from=web-builder /build/static_v2 ./static_v2

EXPOSE 8080

# uvicorn honors $PORT (Cloud Run injects it). One worker keeps the in-memory
# per-conversation client map coherent; scale out with a shared session store.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
