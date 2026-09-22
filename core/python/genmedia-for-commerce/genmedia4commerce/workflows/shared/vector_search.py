# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-memory vector search for the fashion catalogue.

Loads pre-computed embeddings (numpy) and metadata (parquet) from
assets/backend_assets/catalogue/ (downloaded automatically by config.py),
then performs dot-product similarity search entirely in RAM.
"""

import logging
import os
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from dotenv import load_dotenv
from google import genai
from workflows.shared.gemini import embed_gemini

from genmedia4commerce.config import (
    _ASSET_BUCKET,
    _ASSET_PREFIX,
    BACKEND_ASSETS_DIR,
)

logger = logging.getLogger(__name__)

# Load environment variables
config_path = Path(__file__).parent.parent.parent / "config.env"
if config_path.exists():
    load_dotenv(config_path)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
US_REGION = os.getenv("US_REGION", "us-central1")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", os.getenv("MODEL_NAME_GENERATED_6")
)

# Matryoshka dimension truncation — set to None to use full 3072d embeddings.
# Pre-normalized variants available in GCS: 128, 256, 512, 1024.
EMBEDDING_DIMS = 128

_LOCAL_DIR = BACKEND_ASSETS_DIR / "catalogue"
_EMBEDDINGS_FILE = (
    f"embeddings_{EMBEDDING_DIMS}d.npy" if EMBEDDING_DIMS else "embeddings.npy"
)
_METADATA_FILE = "metadata.parquet"

_DATA_FIELDS = [
    "idx",
    "description",
    "img_path",
    "style",
    "audience",
    "season",
    "category",
    "color",
]

# In-memory state
_embeddings: np.ndarray | None = None
_metadata_table = None
_embed_client: genai.Client | None = None


def _load():
    """Load embeddings and metadata into memory."""
    global _embeddings, _metadata_table, _embed_client

    if _embeddings is not None:
        return

    logger.info("Loading catalogue into memory...")

    emb_path = _LOCAL_DIR / _EMBEDDINGS_FILE
    if not emb_path.exists():
        if os.getenv("INTEGRATION_TEST"):
            logger.info(
                "INTEGRATION_TEST is set; skipping GCS catalogue download."
            )
            return
        # Download embeddings from GCS (truncated variant or full)
        logger.info(f"Downloading {_EMBEDDINGS_FILE} from GCS...")
        from google.cloud import storage

        client = storage.Client(project=PROJECT_ID)
        bucket = client.bucket(_ASSET_BUCKET)
        blob = bucket.blob(f"{_ASSET_PREFIX}/catalogue/{_EMBEDDINGS_FILE}")
        _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(emb_path))
        logger.info(f"Downloaded {_EMBEDDINGS_FILE}")

    _embeddings = np.load(emb_path)
    _metadata_table = pq.read_table(_LOCAL_DIR / _METADATA_FILE)
    _embed_client = genai.Client(
        vertexai=True, project=PROJECT_ID, location=US_REGION
    )

    logger.info(
        f"Catalogue loaded: {_embeddings.shape[0]} items, "
        f"{_embeddings.shape[1]}d embeddings, "
        f"{_embeddings.nbytes / 1024 / 1024:.0f} MB"
    )


# Pre-load at import time (download from GCS if needed, then load into RAM)
try:
    _load()
except FileNotFoundError:
    logger.warning(
        "Catalogue files not found — catalog_search will be unavailable"
    )
except Exception as e:
    # Covers auth errors (DefaultCredentialsError, GoogleAuthError), network
    # failures, etc. that can occur when GCP credentials or assets are not
    # configured (e.g. in unit-test environments without ADC).
    logger.warning(
        f"Catalogue could not be loaded at import time "
        f"({type(e).__name__}: {e}) — catalog_search will be unavailable "
        "until credentials and assets are configured"
    )


def embed_query(query: str) -> list[float]:
    """Embed a text query into a vector using the configured embedding model."""
    embedding = embed_gemini(
        [query], client=_embed_client, model=EMBEDDING_MODEL
    )
    return embedding.tolist()


def search_by_vector(
    embedding: list[float],
    k: int = 20,
    filters: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Perform dot-product similarity search using a pre-computed embedding vector.

    Args:
        embedding: The query embedding vector.
        k: Number of results to return.
        filters: Optional dict mapping metadata field names to lists of allowed
            values, e.g. {"audience": ["men", "unisex"]}.

    Returns:
        List of dicts with keys: id, data, score.

    """
    query_vec = np.array(embedding, dtype=np.float32)

    if EMBEDDING_DIMS is not None:
        query_vec = query_vec[:EMBEDDING_DIMS]
        query_vec = query_vec / np.linalg.norm(query_vec)

    scores = _embeddings @ query_vec

    if filters:
        for field, allowed in filters.items():
            allowed_set = {v.lower() for v in allowed}
            col = _metadata_table.column(field).to_pylist()
            mask = np.array([str(v).lower() in allowed_set for v in col])
            scores[~mask] = -np.inf

    top_k = min(k, len(scores))
    top_indices = np.argpartition(scores, -top_k)[-top_k:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

    rows = _metadata_table.take(top_indices.tolist()).to_pydict()

    results = []
    for i in range(len(top_indices)):
        data = {field: rows[field][i] for field in _DATA_FIELDS}
        results.append(
            {
                "id": data.get("idx", ""),
                "data": data,
                "score": float(scores[top_indices[i]]),
            }
        )
    return results


def search(
    query: str, k: int = 20, filters: dict[str, list[str]] | None = None
) -> list[dict]:
    """Embed a query and perform vector similarity search.

    Args:
        query: The query string.
        k: Number of results to return.
        filters: Optional dict mapping metadata field names to lists of allowed
            values, e.g. {"audience": ["men", "unisex"]}.

    Returns:
        List of dicts with keys: id, data, score.

    """
    logger.debug(f"Vector search query: {query}")
    embedding = embed_query(query)
    return search_by_vector(embedding=embedding, k=k, filters=filters)


def search_for_outfit_item(item: dict, k: int = 20) -> dict:
    """Search for real products matching a generated garment item.

    Args:
        item: A dictionary with keys like 'description', 'style', 'color', etc.
        k: Number of candidate products to return.

    Returns:
        The input item enriched with 'matched_products'.

    """
    query = item["description"]

    try:
        results = search(query, k=k)
        item["matched_products"] = results
    except Exception as e:
        item["matched_products"] = []
        item["search_error"] = f"Error searching catalog: {e!s}"

    return item


if __name__ == "__main__":
    import time

    print("Loading catalogue...")
    t0 = time.time()
    _load()
    print(f"Loaded in {time.time() - t0:.1f}s")

    print("\nSearching for 'a red jumper perfect for christmas'...")
    t0 = time.time()
    results = search("a red jumper perfect for christmas", k=5)
    print(f"Search took {(time.time() - t0) * 1000:.0f}ms")
    for res in results:
        print(f"  {res['score']:.4f} | {res['data']['description'][:80]}")
