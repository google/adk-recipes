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
FROM maven:3.9-eclipse-temurin-17 AS build

WORKDIR /build

COPY pom.xml .
RUN mvn -B -q dependency:go-offline

COPY src ./src
RUN mvn -B -q -DskipTests compile dependency:copy-dependencies -DincludeScope=runtime

# Run: JRE only, no Maven.
FROM eclipse-temurin:17-jre

WORKDIR /app

# The ADK dev server loads agents from <source-dir>/target/classes.
COPY --from=build /build/target/classes ./target/classes
COPY --from=build /build/target/dependency ./lib

# Defaults live in .env.example; the agent reads them from .env.
COPY .env.example ./.env

RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app

USER appuser

ENV PORT=8080
EXPOSE 8080

CMD exec java -cp "target/classes:lib/*" com.google.adk.web.AdkWebServer \
    --server.port=${PORT} --adk.agents.source-dir=.
