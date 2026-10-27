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

# Build: a static binary, so the runtime image needs no libc.
FROM golang:1.26 AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/financial-advisor .

# Run: distroless ships CA certificates and a non-root user, nothing else.
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app

COPY --from=build /out/financial-advisor ./financial-advisor

# Defaults live in .env.example; the agent reads them from .env.
COPY .env.example ./.env

# The nonroot image already defaults to this user; stating it keeps the
# non-root requirement visible here.
USER nonroot:nonroot

EXPOSE 8080

# Serves the ADK REST API under /api and the dev UI under /ui on port 8080.
ENTRYPOINT ["/app/financial-advisor"]
CMD ["web", "-port", "8080", "api", "webui"]
