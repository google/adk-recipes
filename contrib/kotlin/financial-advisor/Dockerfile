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

# Build: compile the agent and collect its runtime dependencies.
FROM gradle:9.5.1-jdk17 AS build

WORKDIR /build

COPY settings.gradle.kts build.gradle.kts ./
COPY src ./src
RUN gradle installDist --no-daemon --quiet

# Run: JRE only, no Gradle.
FROM eclipse-temurin:17-jre

WORKDIR /app

COPY --from=build /build/build/install/financial-advisor/lib ./lib

# Defaults live in .env.example; the agent reads them from .env.
COPY .env.example ./.env

RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app

USER appuser

# WebMain binds HOST:PORT. 0.0.0.0 makes the dev server reachable from
# outside the container; it defaults to 127.0.0.1 otherwise.
ENV HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080

CMD ["java", "-cp", "lib/*", "com.google.adk.samples.agents.financialadvisor.WebMainKt"]
