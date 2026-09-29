# Stage 1: Build virtual environment
FROM python:3.11-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final runtime container
FROM python:3.11-slim AS runner
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set environment variables from project configuration
ENV GOOGLE_CLOUD_PROJECT="{{PROJECT_ID}}"
ENV GCP_REGION="{{REGION}}"
ENV TRYON_CATALOG_PATH="{{CATALOG_PATH}}"
ENV TRYON_OUTPUT_BUCKET="{{OUTPUT_BUCKET}}"
ENV TRYON_UPLOAD_BUCKET="{{UPLOAD_BUCKET}}"
ENV GEMINI_IMAGE_MODEL="{{MODEL_NAME}}"
ENV PORT=8080

COPY . .

# Drop root before running the server. The image runs under Cloud Run's
# unprivileged runtime anyway, but an in-image non-root user reduces blast
# radius if the container escapes or is inspected locally.
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
