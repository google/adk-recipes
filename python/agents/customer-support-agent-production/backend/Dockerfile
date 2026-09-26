# Multi-stage build for React frontend + FastAPI backend

# ==============================================================================
# Stage 1: Build React frontend
# ==============================================================================
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./

# Remove .env files to prevent localhost URLs in production build
RUN rm -f .env .env.local .env.development .env.production

RUN npm run build

# ==============================================================================
# Stage 2: Install Python backend dependencies (isolated build layer)
# Uses uv for reproducible installs from uv.lock
# ==============================================================================
FROM python:3.11-slim AS backend-deps

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy workspace lockfile and both pyproject.toml files
# uv needs the full workspace definition to resolve the backend package
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml ./backend/

# Install only the backend package dependencies, no dev extras
# --frozen: fail if uv.lock is out of date (ensures reproducibility)
# --no-dev: exclude dev/test dependencies
# --no-editable: install as regular packages
# --package: install only the named workspace member
RUN uv sync \
    --frozen \
    --no-dev \
    --no-editable \
    --package customer-support-backend

# ==============================================================================
# Stage 3: Runtime image
# ==============================================================================
FROM python:3.11-slim

# Run as non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy the virtual environment from the deps stage
COPY --from=backend-deps /app/.venv ./.venv

# Copy backend application code
COPY backend/app ./app

# Copy built frontend static files
COPY --from=frontend-build /frontend/dist ./static

# Set ownership
RUN chown -R appuser:appuser /app

USER appuser

ENV PORT=8080
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "30"]
