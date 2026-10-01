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

FROM node:22-slim

WORKDIR /app

# --ignore-scripts skips optional native builds (ssh2, cpu-features) that the
# agent never uses and that would need a compiler toolchain in the image.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY app ./app

# Defaults live in .env.example; the ADK CLI reads them from .env.
COPY .env.example ./.env

USER node

ENV PORT=8080
EXPOSE 8080

# The ADK CLI compiles app/agent.ts on startup and serves the dev UI and the
# REST API on one port. Binding 0.0.0.0 also turns off its DNS-rebinding
# guard, which only applies to loopback binds.
CMD exec node_modules/.bin/adk web --host 0.0.0.0 --port ${PORT} .
