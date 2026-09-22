# Container image for the chunk-and-index Cloud Run service.
#
# This service replaces the BigFrames-based chunk_and_index path. Each
# request handles ONE file end-to-end: split + Gemini contextual enrichment
# + VS2 chunk + document upsert. The pipeline orchestrator fans out files
# in parallel via authenticated HTTP, and Cloud Run autoscales instances
# (max_instance_request_concurrency=1).
#
# Build from project root (context must be repo root):
#   gcloud builds submit \
#     --config=data_ingestion_pipeline/chunk_index_service/cloudbuild.yaml \
#     --substitutions=_IMAGE="$CHUNK_INDEX_SERVICE_IMAGE" \
#     --project="$GOOGLE_CLOUD_PROJECT" .

FROM python:3.11-slim

WORKDIR /app

COPY data_ingestion_pipeline/chunk_index_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the shared src package so we can reuse retry helpers etc.
# from src.utils (matches the preprocess service layout).
COPY src/ src/

# Copy the Cloud Run service entrypoint
COPY data_ingestion_pipeline/chunk_index_service/main.py .

ENV PYTHONPATH=/app

CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 900
