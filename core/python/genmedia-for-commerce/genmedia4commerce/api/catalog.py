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

"""The local catalogue: search and images."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from genmedia4commerce.core import catalog, catalog_search
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.media import sniff_mime
from genmedia4commerce.studio import picks

__all__ = ["router"]

router = APIRouter(prefix="/api/catalog", tags=["Catalog"])

_CACHE = {"Cache-Control": "public, max-age=86400"}


class SearchBody(BaseModel):
    """``POST /api/catalog/search`` body (an empty query: the featured
    products)."""

    query: str = ""
    k: int = Field(default=8, ge=1, le=catalog_search.MAX_K)
    audience: list[str] | None = None
    family: str = ""


@router.post("/search")
async def search(body: SearchBody) -> dict:
    """``{items: [catalogue row + ref + thumb_url]}``, best first."""
    rows = await asyncio.to_thread(
        catalog_search.find,
        body.query,
        audience=body.audience or (),
        family=body.family,
        k=body.k,
    )
    return {
        "items": [
            {
                **r,
                "ref": catalog.ref(r["id"]),
                "thumb_url": picks.thumb(catalog.ref(r["id"]), inline=False),
            }
            for r in rows
        ]
    }


@router.get("/image/{item_id}/{n}")
async def image(
    item_id: Annotated[str, Path(max_length=64)],
    n: Annotated[int, Path(ge=0, le=64)],
    w: Annotated[int | None, Query(ge=32, le=1024)] = None,
) -> Response:
    """Image ``n`` of a catalogue item, or with ``w`` a WebP thumbnail at
    most ``w`` pixels a side. Browsers may cache it for a day."""
    try:
        if w is None:
            data = await asyncio.to_thread(catalog.image, item_id, n)
        else:
            data = await asyncio.to_thread(catalog.thumbnail, item_id, n, w)
    except InputError as exc:
        raise HTTPException(404, "Not Found") from exc
    return Response(data, media_type=sniff_mime(data), headers=_CACHE)
