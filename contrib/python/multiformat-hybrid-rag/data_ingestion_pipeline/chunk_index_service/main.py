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

"""Cloud Run service that chunks one document and indexes it into VS2.

The pipeline orchestrator dispatches one HTTP request per file via a
ThreadPoolExecutor (same pattern as preprocess_service). Each request:

    1. Splits the document text with a markdown-aware splitter
    2. Calls Gemini per chunk to generate contextual enrichment
    3. Deletes any pre-existing VS2 entries for this file_id
    4. Pushes the new chunks to the VS2 chunks collection
    5. Upserts the full document to the VS2 documents collection
    6. Returns the chunk rows so the orchestrator can MERGE them into BQ

Returning per-file *atomic* success is the contract — when the service
responds 200 with `{"indexed": true}`, both VS2 collections are guaranteed
populated for this file. If anything fails, the orchestrator skips the BQ
insert for this file and the next run will retry naturally.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from google import genai
from google.api_core import exceptions as gax
from google.cloud import vectorsearch_v1beta
from google.genai import types as genai_types
from langchain_text_splitters import MarkdownTextSplitter

from src.utils import CHUNK_ID_SEPARATOR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration from environment variables (set by Terraform) ───────────
GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
REGION = os.environ.get("REGION", "us-central1")
# Gemini endpoint, not the data region: the gemini-3.x family is published
# only on the `global` endpoint and 404s from a regional one.
MODEL_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION_MODELS", "global")
GEMINI_MODEL = os.environ["CONTEXTUAL_CHUNKING_GEMINI_MODEL"]
COLLECTION_PATH = os.environ["VS_COLLECTION_PATH"]
DOCUMENTS_COLLECTION_PATH = os.environ["VS_DOCUMENTS_COLLECTION_PATH"]
VS_BATCH_SIZE = int(os.environ.get("VS_BATCH_SIZE", "250"))
ENRICH_THREADS = int(os.environ.get("ENRICH_THREADS", "16"))

# Context-window settings for chunk enrichment.
#
# If the whole document fits under MAX_FULL_DOC_CHARS, every chunk gets
# the entire doc as context (best quality). For documents larger than
# that, we fall back to a per-chunk sliding window: each chunk's context
# is the WINDOW_PER_SIDE_CHARS chars before + the chunk + the same
# number of chars after. This way chunks at the end of long docs (e.g.
# the multi-MB catalog JSONs) get LOCAL context, instead of always being
# described in terms of the FIRST few thousand characters of the doc.
MAX_FULL_DOC_CHARS = int(os.environ.get("MAX_FULL_DOC_CHARS", "500000"))
WINDOW_PER_SIDE_CHARS = int(os.environ.get("WINDOW_PER_SIDE_CHARS", "50000"))

CONTEXTUAL_SYSTEM_PROMPT = """\
Sei un esperto di analisi documentale.

**Input**
- Un frammento di testo estratto da un documento
- Il documento originale completo

**Compito**
Scrivi fino a 4 frasi che spieghino il contesto di questo frammento all'interno \
del documento. Chi legge solo il frammento deve poter capire di cosa si parla.

**Regole**
- Non ripetere contenuti già presenti nel frammento
- Descrivi brevemente cosa c'è prima e dopo per orientare il lettore
- Ignora metadati (autori, date, copyright, contatti)
- Scrivi nella stessa lingua del frammento
- Restituisci solo le frasi di contesto, niente altro"""

# Transient errors that warrant a retry against VS2 / Gemini. Anything
# else (auth, validation) bubbles up so the orchestrator marks the file
# as failed instead of silently retrying forever.
_RETRYABLE = (
    gax.ResourceExhausted,
    gax.ServiceUnavailable,
    gax.DeadlineExceeded,
    gax.InternalServerError,
    gax.GatewayTimeout,
    gax.BadGateway,
    gax.Aborted,
    gax.RetryError,
    ConnectionError,
    TimeoutError,
)


# ── Initialize clients once (reused across requests) ─────────────────────
genai_client = genai.Client(
    vertexai=True,
    project=GOOGLE_CLOUD_PROJECT,
    location=MODEL_LOCATION,
    http_options=genai_types.HttpOptions(
        retry_options=genai_types.HttpRetryOptions(
            initial_delay=1.0,
            attempts=5,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    ),
)
vs_client = vectorsearch_v1beta.DataObjectServiceClient()
vs_search_client = vectorsearch_v1beta.DataObjectSearchServiceClient()


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info(
        "Chunk-index service started (project=%s, model=%s, collection=%s)",
        GOOGLE_CLOUD_PROJECT,
        GEMINI_MODEL,
        COLLECTION_PATH,
    )
    yield


app = FastAPI(title="Chunk-Index Service", lifespan=lifespan)


def _gemini_enrich(doc_context: str, chunk_text: str) -> str:
    """Single Gemini enrichment call. The SDK client is configured with
    HttpRetryOptions, so transient errors are retried automatically."""
    resp = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=(
            f"DOCUMENT:\n```\n{doc_context}\n```\n\n"
            f"CHUNK:\n```\n{chunk_text}\n```\n\n"
            "Generate the context useful to understand the chunk."
        ),
        config={
            "temperature": 0,
            "system_instruction": CONTEXTUAL_SYSTEM_PROMPT,
        },
    )
    return resp.text or ""


def _enrich_chunk(doc_context: str, chunk_text: str) -> str:
    """Ask Gemini for context that situates this chunk in the document.

    Returns "" if Gemini failed all retries — chunks remain searchable
    on their own text, just without the additional context prefix.
    Empty-context chunks are counted upstream so we can monitor
    enrichment health.
    """
    try:
        return _gemini_enrich(doc_context, chunk_text)
    except Exception as e:
        logger.warning("Gemini enrichment failed after retries: %s", e)
        return ""


def _chunk_context(text: str, chunk: str, chunk_start: int) -> str:
    """Build the context string sent to Gemini for one chunk.

    - If the doc fits in MAX_FULL_DOC_CHARS → use the whole doc as
      context (best quality for short/medium files).
    - Otherwise → sliding window of WINDOW_PER_SIDE_CHARS before and
      after the chunk's position, so chunks at the end of long docs get
      LOCAL context instead of always being described in terms of the
      doc's opening.
    """
    if len(text) <= MAX_FULL_DOC_CHARS:
        return text
    start = max(0, chunk_start - WINDOW_PER_SIDE_CHARS)
    end = min(len(text), chunk_start + len(chunk) + WINDOW_PER_SIDE_CHARS)
    return text[start:end]


def _split_and_enrich(
    text: str, chunk_size: int, chunk_overlap: int
) -> list[dict]:
    splitter = MarkdownTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    chunks = splitter.split_text(text)
    if not chunks:
        return []

    # Locate each chunk's start position in the original text so we can
    # build a per-chunk context window. Walk forward from `cursor` so
    # repeated substrings (common in templated docs) match in order.
    chunk_starts: list[int] = []
    cursor = 0
    for chunk in chunks:
        idx = text.find(chunk, cursor)
        if idx < 0:
            # Splitter normalisation can drop trailing whitespace; fall
            # back to a fresh search so we never leave a None-ish position.
            idx = text.find(chunk)
            if idx < 0:
                idx = cursor
        chunk_starts.append(idx)
        cursor = idx + len(chunk) - chunk_overlap

    results: list[dict] = [{}] * len(chunks)
    with ThreadPoolExecutor(max_workers=ENRICH_THREADS) as pool:
        futures = {
            pool.submit(
                _enrich_chunk,
                _chunk_context(text, chunk, chunk_starts[idx]),
                chunk,
            ): (idx, chunk)
            for idx, chunk in enumerate(chunks)
        }
        for fut in as_completed(futures):
            idx, chunk_text = futures[fut]
            results[idx] = {
                "chunk_index": idx,
                "chunk_text": chunk_text,
                "context": fut.result(),
            }
    return results


def _retry_call(fn, *args, attempts: int = 6, base_sleep: float = 2.0):
    """Run fn(*args) with exponential backoff on transient API errors."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn(*args)
        except _RETRYABLE as e:
            last_exc = e
            wait = min(60.0, base_sleep * (2**attempt))
            logger.warning(
                "Transient %s on attempt %d, sleeping %.1fs",
                type(e).__name__,
                attempt + 1,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Failed after {attempts} retries: {type(last_exc).__name__}: {last_exc}"
    )


def _delete_existing_chunks(file_id: str) -> int:
    """Delete any VS2 chunks belonging to this file_id (idempotent re-chunk)."""
    page_token = None
    names: list[str] = []
    while True:
        request = vectorsearch_v1beta.QueryDataObjectsRequest(
            parent=COLLECTION_PATH,
            filter={"file_id": {"$in": [file_id]}},
            **({"page_token": page_token} if page_token else {}),
        )
        response = vs_search_client.query_data_objects(request)
        names.extend(obj.name for obj in response.data_objects)
        if not response.next_page_token:
            break
        page_token = response.next_page_token

    if not names:
        return 0

    delete_requests = [
        vectorsearch_v1beta.DeleteDataObjectRequest(name=name) for name in names
    ]
    request = vectorsearch_v1beta.BatchDeleteDataObjectsRequest(
        parent=COLLECTION_PATH,
        requests=delete_requests,
    )
    _retry_call(vs_client.batch_delete_data_objects, request)
    return len(names)


def _push_chunks_to_vs2(file_id: str, gcs_uri: str, chunks: list[dict]) -> None:
    """Push enriched chunks to the VS2 chunks collection in batches."""
    payload = [
        {
            "data_object_id": (
                f"{file_id}{CHUNK_ID_SEPARATOR}{c['chunk_index']}"
            ),
            "data_object": {
                "data": {
                    "file_id": file_id,
                    "chunk_id": (
                        f"{file_id}{CHUNK_ID_SEPARATOR}{c['chunk_index']}"
                    ),
                    "chunk_text": (
                        f"{c['context']}\n\n{c['chunk_text']}"
                        if c["context"].strip()
                        else c["chunk_text"]
                    ),
                    "gcs_uri": gcs_uri,
                },
                "vectors": {},
            },
        }
        for c in chunks
    ]

    for start in range(0, len(payload), VS_BATCH_SIZE):
        batch = payload[start : start + VS_BATCH_SIZE]
        request = vectorsearch_v1beta.BatchCreateDataObjectsRequest(
            parent=COLLECTION_PATH,
            requests=batch,
        )
        _retry_call(vs_client.batch_create_data_objects, request)


def _upsert_document_to_vs2(file_id: str, gcs_uri: str, content: str) -> None:
    """Upsert this file's full text into the VS2 documents collection.

    Try `update_data_object` first (atomic in-place replace). If the
    object doesn't exist yet (first time we see this file_id), fall
    back to `batch_create_data_objects`.

    Why not delete-then-create like before: a crash between the delete
    and the create would leave the documents collection with no entry
    for the file_id, breaking full-document fetch until the next run.
    Update is atomic, so the only racy path is the very first insert.
    """
    name = f"{DOCUMENTS_COLLECTION_PATH}/dataObjects/{file_id}"
    data_object = {
        "name": name,
        "data": {"file_id": file_id, "gcs_uri": gcs_uri, "content": content},
        "vectors": {},
    }

    try:
        _retry_call(
            vs_client.update_data_object,
            vectorsearch_v1beta.UpdateDataObjectRequest(
                data_object=data_object
            ),
        )
        return
    except gax.NotFound:
        pass  # first time we see this file_id → create

    create_request = vectorsearch_v1beta.BatchCreateDataObjectsRequest(
        parent=DOCUMENTS_COLLECTION_PATH,
        requests=[
            {
                "data_object_id": file_id,
                "data_object": {
                    "data": data_object["data"],
                    "vectors": {},
                },
            }
        ],
    )
    _retry_call(vs_client.batch_create_data_objects, create_request)


@app.post("/")
async def handle_request(request: Request):
    """Chunk one file and index it into VS2.

    Request:
        {file_id, gcs_uri, content, chunk_size, chunk_overlap}

    Response (200):
        {file_id, indexed: true, chunks: [{chunk_index, chunk_text, context}, ...]}

    Failures bubble up as 5xx so the orchestrator can mark the file as
    not-yet-indexed and retry on the next run.
    """
    body = await request.json()
    file_id = body["file_id"]
    gcs_uri = body["gcs_uri"]
    content = body["content"] or ""
    chunk_size = int(body["chunk_size"])
    chunk_overlap = int(body["chunk_overlap"])

    if not content.strip():
        return {"file_id": file_id, "indexed": True, "chunks": []}

    try:
        chunks = _split_and_enrich(content, chunk_size, chunk_overlap)
        if not chunks:
            return {"file_id": file_id, "indexed": True, "chunks": []}

        _delete_existing_chunks(file_id)
        _push_chunks_to_vs2(file_id, gcs_uri, chunks)
        _upsert_document_to_vs2(file_id, gcs_uri, content)
    except Exception as e:
        logger.error("Failed to chunk-and-index %s: %s", file_id, e)
        raise HTTPException(
            status_code=500, detail=f"{type(e).__name__}: {e}"
        ) from e

    failed_enrichments = sum(1 for c in chunks if not c["context"].strip())
    if failed_enrichments:
        logger.warning(
            "%s: %d/%d chunks shipped with empty context",
            file_id,
            failed_enrichments,
            len(chunks),
        )

    return {
        "file_id": file_id,
        "indexed": True,
        "chunks": chunks,
        "failed_enrichments": failed_enrichments,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
