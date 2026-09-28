# Copyright 2026 Google LLC
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

"""REST API endpoints for catalog search."""

import asyncio
import logging

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel
from workflows.shared.gcs_utils import get_https
from workflows.shared.vector_search import search

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/catalog",
    tags=["Catalog"],
)


class CatalogSearchRequest(BaseModel):
    """Request schema for catalog search."""

    query: str
    k: int = 12


def _format_results(results):
    items = []
    for item in results:
        data = item.get("data", {})
        img_path = data.get("img_path", "")
        items.append(
            {
                "id": item.get("id", ""),
                "description": data.get("description", ""),
                "img_url": get_https(img_path)
                if img_path.startswith("gs://")
                else img_path,
                "gs_uri": img_path if img_path.startswith("gs://") else "",
                "category": data.get("category", ""),
                "color": data.get("color", ""),
                "style": data.get("style", ""),
                "audience": data.get("audience", ""),
                "score": item.get("score", 0),
            }
        )
    return items


@router.post("/search")
async def catalog_search(req: CatalogSearchRequest):
    """Search the product catalogue across all audiences."""
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, search, req.query, req.k)
    except Exception as e:
        logger.error(f"Catalog search failed: {e}")
        return {"results": []}

    return {"results": _format_results(results)}


@router.get("/image")
async def catalog_image(url: str = Query(...)):
    """Proxy a GCS image using the authenticated GCS client."""
    from google.cloud import storage as gcs_storage

    prefix = "https://storage.cloud.google.com/"
    if not url.startswith(prefix):
        return Response(status_code=400)
    path = url[len(prefix) :]
    bucket_name, _, blob_name = path.partition("/")
    if not blob_name:
        return Response(status_code=400)

    try:

        def _download():
            gcs_client = gcs_storage.Client()
            blob = gcs_client.bucket(bucket_name).blob(blob_name)
            return blob.download_as_bytes()

        data = await asyncio.get_event_loop().run_in_executor(None, _download)
        ext = blob_name.rsplit(".", 1)[-1].lower() if "." in blob_name else ""
        ext_map = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }
        content_type = ext_map.get(ext, "image/jpeg")
        return Response(content=data, media_type=content_type)
    except Exception as e:
        logger.error(f"Image proxy failed for {url}: {e}")
        return Response(status_code=502)
