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

"""Cloud Run service for BQ REMOTE FUNCTION — preprocesses GCS files.

Thin HTTP adapter that receives batches of GCS URIs from BigQuery and
delegates all processing to the shared pipeline/ package. The same
parsing and classification code runs here and in the local pipeline.

BQ REMOTE FUNCTION protocol:
    Request:  {"calls": [["gs://bucket/file1.pdf"], ["gs://bucket/file2.doc"], ...]}
    Response: {"replies": ["{json_result_1}", "{json_result_2}", ...]}

Each reply is a JSON string: {"file_id": "...", "text": "...", "relevant": bool}

LibreOffice is pre-installed in the Docker image for DOC/PPT/RTF conversion.
"""

import hashlib
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from google import genai
from google.cloud import storage
from google.genai import types as genai_types

from src.document_preprocessing.document_relevance_classifier import (
    classify_relevance,
)
from src.document_preprocessing.parser import (
    PARSEABLE_MIMES,
    parse,
    quick_raw_text,
)

# Min chars of raw text we trust as "enough to judge relevance from". Below
# this we assume the cheap extraction missed content (e.g. scanned PDF) and
# fall through to the full Gemini extraction before classifying.
_MIN_RAW_FOR_CLASSIFY = 200

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration from environment variables (set by Terraform) ───────────
GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
REGION = os.environ.get("REGION", "us-central1")
GCS_BUCKET = os.environ["GCS_BUCKET"]

# MIME type → file extension for temp files (parsers need a real file path)
_MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/rtf": ".rtf",
    "text/html": ".html",
    "application/json": ".json",
    "application/jsonl": ".jsonl",
}

# ── Initialize clients once (reused across requests) ─────────────────────
storage_client = storage.Client(project=GOOGLE_CLOUD_PROJECT)
genai_client = genai.Client(
    vertexai=True,
    project=GOOGLE_CLOUD_PROJECT,
    location="global",
    http_options=genai_types.HttpOptions(
        retry_options=genai_types.HttpRetryOptions(
            initial_delay=1.0,
            attempts=5,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    ),
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info(
        "Preprocess service started (project=%s, bucket=%s)",
        GOOGLE_CLOUD_PROJECT,
        GCS_BUCKET,
    )
    yield


app = FastAPI(title="Preprocess Service", lifespan=lifespan)


def _download_to_temp(gcs_uri: str) -> tuple[str | None, str]:
    """Download the GCS object to a tempfile.

    Returns (tmp_path, content_type). tmp_path is None for plain-text /
    markdown blobs that we read directly without a parser tempfile.
    """
    blob_name = gcs_uri.replace(f"gs://{GCS_BUCKET}/", "")
    blob = storage_client.bucket(GCS_BUCKET).blob(blob_name)
    blob.reload()
    content_type = blob.content_type or ""

    if content_type not in PARSEABLE_MIMES:
        return None, content_type

    raw_bytes = blob.download_as_bytes()
    ext = _MIME_TO_EXT.get(content_type, "")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw_bytes)
        return tmp.name, content_type


def _is_scanner_probe(gcs_uri: str) -> bool:
    """Check if the file is a web security scanner probe (dotfile)."""
    basename = gcs_uri.rstrip("/").rsplit("/", 1)[-1]
    return basename.startswith(".")


def process_file(gcs_uri: str) -> str:
    """Process a single file: download → maybe early-classify → parse → return JSON.

    Optimization: if the parser supports `quick_raw_text` and returns
    enough text to judge (>= _MIN_RAW_FOR_CLASSIFY chars), we classify
    relevance on the raw text BEFORE running the expensive markdown
    conversion (Gemini multimodal per page, LibreOffice, etc.).
    Irrelevant files get a stub row immediately and skip the conversion.

    Sparse-or-no raw text (typical of scanned PDFs and image-only HTMLs)
    falls through to the full extraction path so we don't drop content
    that's only visible to Gemini multimodal.

    Returns a JSON string with file_id, text, relevance flag, and error.
    On failure, error is non-null; text is "" and relevant is False so
    the row is excluded from chunking until the error is resolved.
    """
    file_id = hashlib.md5(gcs_uri.encode()).hexdigest()

    if _is_scanner_probe(gcs_uri):
        logger.info("Skipping scanner probe: %s", gcs_uri)
        return json.dumps(
            {
                "file_id": file_id,
                "text": "",
                "relevant": False,
                "error": None,
            }
        )

    tmp_path: str | None = None
    try:
        tmp_path, _content_type = _download_to_temp(gcs_uri)

        # Plain text / markdown: no parser path, no Gemini conversion to skip.
        # Just download as text and classify (same as before).
        if tmp_path is None:
            blob_name = gcs_uri.replace(f"gs://{GCS_BUCKET}/", "")
            text = (
                storage_client.bucket(GCS_BUCKET)
                .blob(blob_name)
                .download_as_text()
            )
            relevant = classify_relevance(text, genai_client)
            return json.dumps(
                {
                    "file_id": file_id,
                    "text": text,
                    "relevant": relevant,
                    "error": None,
                }
            )

        # Try the cheap raw-text path. If we get enough characters, classify
        # on raw and short-circuit irrelevant files BEFORE the Gemini call.
        raw = quick_raw_text(tmp_path) or ""
        if len(raw.strip()) >= _MIN_RAW_FOR_CLASSIFY:
            if not classify_relevance(raw, genai_client):
                logger.info("Early-skip irrelevant file: %s", gcs_uri)
                return json.dumps(
                    {
                        "file_id": file_id,
                        "text": "",
                        "relevant": False,
                        "error": None,
                    }
                )
            text = parse(tmp_path, genai_client=genai_client)
            return json.dumps(
                {
                    "file_id": file_id,
                    "text": text,
                    "relevant": bool(text.strip()),
                    "error": None,
                }
            )

        # Sparse raw text (scanned PDF, image-only page, parser without
        # quick_raw_text): fall through to today's behaviour — full parse
        # then classify on the result.
        text = parse(tmp_path, genai_client=genai_client)
        relevant = classify_relevance(text, genai_client)
        return json.dumps(
            {
                "file_id": file_id,
                "text": text,
                "relevant": relevant,
                "error": None,
            }
        )

    except Exception as e:
        logger.error("Failed to process %s: %s", gcs_uri, e)
        return json.dumps(
            {
                "file_id": file_id,
                "text": "",
                "relevant": False,
                "error": f"{type(e).__name__}: {e}",
            }
        )
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.post("/")
async def handle_request(request: Request):
    """Handle one preprocess request.

    The wire format keeps the legacy {"calls": [[uri]]} list-of-lists
    shape (originally from BigQuery REMOTE FUNCTION), but the orchestrator
    now sends single-URI batches via HTTP fanout — so we process them
    serially in this handler. Cross-file parallelism happens at the
    orchestrator level (ThreadPoolExecutor of 200) and at the Cloud Run
    autoscaler level (concurrency=2 per instance x N instances).
    """
    req = await request.json()
    calls = req.get("calls", [])
    if not calls:
        return {"replies": []}
    replies = [process_file(call[0]) for call in calls]
    return {"replies": replies}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
