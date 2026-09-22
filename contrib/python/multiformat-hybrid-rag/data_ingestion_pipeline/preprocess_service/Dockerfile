# Container image for the preprocessing Cloud Run service.
#
# This service backs the BQ REMOTE FUNCTION `preprocess_file()`. BigQuery
# calls it via HTTP for each file that needs preprocessing. Unlike the KFP
# pipeline image, this container includes LibreOffice for converting legacy
# Office formats (DOC, PPT, XLS, RTF) to PDF before Gemini extraction.
#
# Supported formats:
#   - PDF:  pymupdf page split + Gemini multimodal (per page)
#   - DOCX/PPTX/XLSX: python-docx/python-pptx/openpyxl (native Python)
#   - DOC/PPT/XLS/RTF: LibreOffice → PDF → Gemini multimodal
#   - HTML: BeautifulSoup text extraction + Gemini
#   - Text/Markdown: read as UTF-8 directly
#
# Build from project root (context must be repo root for COPY paths):
#   docker build -f data_ingestion_pipeline/preprocess_service/Dockerfile \
#     -t preprocess-service:latest .
#
# Or via Cloud Build:
#   gcloud builds submit \
#     --config=data_ingestion_pipeline/preprocess_service/cloudbuild.yaml \
#     --substitutions=_IMAGE="$PREPROCESS_SERVICE_IMAGE" \
#     --project="$GOOGLE_CLOUD_PROJECT" .

FROM python:3.11-slim

# Install LibreOffice for legacy Office format conversion (DOC, PPT, XLS, RTF).
# Only core + writer/impress/calc are needed — no GUI components.
# default-jre-headless is required to convert legacy binary OLE formats
# (notably .ppt → .pptx); without Java, soffice silently writes no output.
# Clean up apt cache to keep image size down.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-impress \
        libreoffice-calc \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install only the dependencies the preprocess service actually needs.
# Using a slim requirements.txt avoids pulling in google-adk (which
# auto-discovers agents and hijacks FastAPI routes), bigframes, kfp,
# and other heavy packages not needed for document parsing.
COPY data_ingestion_pipeline/preprocess_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the shared src package — all parsing logic lives here.
# This is the key advantage over @bpd.remote_function: we have access
# to the full src/ package including all document parsers.
COPY src/ src/

# .env.example, not .env: .env is gitignored and therefore absent from the
# Cloud Build context. Provides model names, embedding config, etc. as
# non-secret defaults; real values arrive as environment variables.
# (.gcloudignore negates the gitignore exclusion so Cloud Build can access it.)
COPY .env.example .

# Copy the Cloud Run service entrypoint
COPY data_ingestion_pipeline/preprocess_service/main.py .

ENV PYTHONPATH=/app

# Run with uvicorn: single worker, async event loop.
# Each request processes a batch of files from BQ, with internal
# ThreadPoolExecutor for per-file parallelism.
CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 900
