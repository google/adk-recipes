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

"""Tools of the Studio agent.

Each tool edits the turn's working state (``studio.turn.current().st``) and
records a patch for the app. Their responses go into the conversation
history, so refs returned here (catalogue alternatives, job ids) stay
available to later turns. The settings tool lives in
``studio.agent_settings``, the spin sets tool in ``studio.agent_spin``,
the job spec ``generate`` starts in ``studio.agent_spec``.

``generate`` and ``remake_video`` (a "Make video" of one of Video
Try-On's images) start a job only with what the user has seen: when the
agent changed the inputs, the page or the variations in the same turn, or
asks for another page, they answer ``needs_confirmation`` and the model
asks first (``studio.tool_order``); the job starts through the cost guards
(``studio.quota``). ADK runs the calls of one model response as concurrent
tasks on the event loop (async tools on the loop; sync tools inline too, as
the agent sets no tool thread pool), which ``tool_order`` accounts for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from genmedia4commerce import tools
from genmedia4commerce.core import catalog
from genmedia4commerce.core.errors import (
    USER_ERRORS,
    GenMediaError,
    user_message,
)
from genmedia4commerce.studio import jobs, quota, turn
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio.agent_settings import update_settings
from genmedia4commerce.studio.agent_spec import job_spec
from genmedia4commerce.studio.agent_spin import (
    NOT_FOR_SPIN,
    ONE_PHOTO,
    choose_spin_set,
    spin_set_names,
)
from genmedia4commerce.studio.tool_order import (
    needs_confirmation,
    setup_tool,
    start_once,
)

__all__ = ["NEEDS_CONFIRMATION", "TOOLS"]

logger = logging.getLogger(__name__)

#: Results ``check_generation`` returns, and how long it may wait.
_MAX_RESULTS = 3
_MAX_WAIT_S = 60
_POLL_S = 3.0

_FIELDS = rules.SLOT_FIELDS + rules.LIST_FIELDS
#: What ``generate`` answers after the agent changed the inputs, the page
#: or the variations this turn, or for another page.
NEEDS_CONFIRMATION = {
    "status": "needs_confirmation",
    "message": "You changed the studio's inputs, page or variations in this "
    'turn: say in one line what is set up and ask "Shall I generate?". '
    "Generate only when the user confirms in their next message.",
}
#: What ``remake_video`` answers when Video Try-On has no try-on image.
NO_IMAGES = (
    "Video Try-On has no try-on images yet: a video try-on from the person "
    "and the products makes them."
)


def _st() -> dict[str, Any]:
    return turn.current().st


def _put(field: str, item: dict[str, str], replace_index: int = -1) -> None:
    """Put an item in a field of the working state and patch the app."""
    st = _st()
    if field in rules.SLOT_FIELDS:
        st[field] = item
        turn.patch("set", {"field": field, "item": item})
        return
    items = st.setdefault(rules.state_key(field), [])
    if 0 <= replace_index < len(items):
        items[replace_index] = item
    elif len(items) < 8:
        items.append(item)
    turn.patch(
        "add", {"field": field, "item": item, "replace_index": replace_index}
    )


def open_capability(capability: str) -> dict[str, Any]:
    """Show the page of a capability.

    Args:
        capability: "tryon" (Photo Try-On: any try-on or "VTO" request),
            "video_tryon" (Video Try-On: the person dressed in the
            products, then animated; only when the user asks for a video or
            an animation) or "spin" (360° Spin).
    """
    if capability not in jobs.CAPABILITIES:
        return {"error": "capability must be tryon, video_tryon or spin"}
    if _st().get("capability") != capability:  # a switch needs confirming
        _st()["capability"] = capability
        turn.patch("capability", capability)
    return {"page": capability}


@setup_tool
async def find_product(
    query: str, audience: str = "", replace_index: int = -1
) -> dict[str, Any]:
    """Search the whole catalogue (the featured products and the others)
    and put the best match in the try-on products. Not for 360° Spin: a
    catalogue product has one photo (use choose_spin_set).

    Args:
        query: A rich description, e.g. "black crew-neck cotton t-shirt".
        audience: "women", "men", "unisex" or "" for any.
        replace_index: Index of the product to replace, or -1 to add.
    """
    if _st().get("capability") == "spin":
        return {"error": NOT_FOR_SPIN, "spin_sets": await spin_set_names()}
    if not query.strip():
        return {"error": "Say what to look for."}
    res = await tools.catalog_search(query=query, k=3, audience=audience)
    found = [
        {"label": str(i["title"]), "ref": str(i["ref"])}
        for i in res.get("items") or []
    ]
    if not found:
        return {
            "error": res.get("error") or f"No catalogue match for {query!r}"
        }
    uri, desc = await asyncio.to_thread(sm.to_gcs, found[0]["ref"])
    best = {"label": found[0]["label"], "ref": uri}
    _put("product", best, replace_index)
    return {"used": best, "description": desc, "alternatives": found[1:]}


@setup_tool
async def use_image(
    ref: str, field: str, label: str = "", replace_index: int = -1
) -> dict[str, Any]:
    """Put an image the conversation knows about into a studio field: an
    upload, a catalogue item or a generated result (by its ref).

    Args:
        ref: The image's ref, e.g. "gs://…/outputs/front-0.png".
        field: "person", "reference_model" (only the user's own photo of a
            model wearing the look), "product", "spin" (the user's
            photos of the product to spin; a single catalogue photo only
            when the user insists).
        label: A short name for the image (optional).
        replace_index: For product or spin, the index to replace, or -1.
    """
    if field not in _FIELDS:
        return {"error": f"field must be one of {', '.join(_FIELDS)}"}
    ref = await asyncio.to_thread(sm.resolve, ref)
    studio_id = turn.current().studio_id  # never another studio's files
    if not await asyncio.to_thread(sm.allowed_ref, ref, studio_id):
        return {"error": f"{ref!r} is not an image this studio can use"}
    if field == "reference_model" and rules.is_catalogue_image(ref):
        return {"error": rules.REFERENCE_MODEL_HINT}
    if catalog.is_ref(ref) and not label:
        label = catalog.item(catalog.parse_ref(ref)[0])["title"]
    uri, _desc = await asyncio.to_thread(sm.to_gcs, ref)
    item = {"label": label or uri.rsplit("/", 1)[-1], "ref": uri}
    _put(field, item, replace_index)
    out: dict[str, Any] = {"used": item, "field": field}
    if field == "spin" and len(_st().get("spin") or []) == 1:
        out["warning"] = ONE_PHOTO
    return out


@setup_tool
async def choose_person(name: str) -> dict[str, Any]:
    """Use a gallery model as the person.

    Args:
        name: The model's name, or words from it.
    """
    models = await asyncio.to_thread(catalog.models)
    words = name.lower().split()
    for m in models:
        if all(w in m["title"].lower() for w in words):
            ref = catalog.ref(m["id"])
            uri, desc = await asyncio.to_thread(sm.to_gcs, ref)
            item = {"label": m["title"], "ref": uri}
            _put("person", item)
            return {"person": item, "description": desc}
    names = [m["title"] for m in models]
    return {"error": f"No gallery model matches {name!r}", "models": names}


def remove_product(index: int) -> dict[str, Any]:
    """Remove one product.

    Args:
        index: Index of the product, from the studio state.
    """
    items = _st().setdefault("products", [])
    if not 0 <= index < len(items):
        return {"error": f"There are {len(items)} products."}
    removed = items.pop(index)
    turn.patch("remove", {"field": "product", "index": index})
    return {"removed": removed, "products": items}


def clear(field: str) -> dict[str, Any]:
    """Empty a studio field.

    Args:
        field: "person", "reference_model", "product" or "spin".
    """
    if field not in _FIELDS:
        return {"error": f"field must be one of {', '.join(_FIELDS)}"}
    st = _st()
    st[rules.state_key(field)] = [] if field in rules.LIST_FIELDS else None
    turn.patch("clear", {"field": field})
    return {"cleared": field}


async def _job_status() -> dict[str, Any]:
    """The status of the studio's latest job (``{}`` when there is none)."""
    job_id = (_st().get("job") or {}).get("job_id")
    return await asyncio.to_thread(jobs.status, job_id) if job_id else {}


async def generate(capability: str = "") -> dict[str, Any]:
    """Start a generation with what is in the studio (one at a time); its
    results are added to the conversation when it finishes. Only with what
    the user has seen: after you changed the inputs, the page or the
    variations in this turn, or for another page than the current one (which
    it opens), it answers "needs_confirmation" and starts nothing.

    Args:
        capability: "tryon" (any try-on or "VTO"), "video_tryon" (only when
            the user asks for a video or an animation), "spin", or "" for the
            current page.
    """
    cap = capability if capability in jobs.CAPABILITIES else ""
    cap = cap or str(_st().get("capability") or "tryon")
    if await needs_confirmation(cap):
        open_capability(cap)  # the user sees the page it would generate
        return dict(NEEDS_CONFIRMATION)
    spec, missing = job_spec(cap)
    if missing:
        return {"error": f"Cannot generate yet: add {missing} first."}
    return await _start(spec, "generate")


async def remake_video(image: int) -> dict[str, Any]:
    """Make the videos of one of Video Try-On's try-on images again ("redo
    the video for image 2"): only that image's videos are replaced, the
    other videos and the images stay. Like generate, only with what the
    user has seen: after you changed the studio in this turn, or from
    another page (which it opens), it answers "needs_confirmation" and
    starts nothing.

    Args:
        image: The image's number as the studio shows it, from 1 ("Image
            1"), in the order of the video try-on images.
    """
    images = (_st().get("images") or {}).get("video_tryon") or []
    uris = [
        str(i.get("uri") or "") if isinstance(i, dict) else "" for i in images
    ]
    if not any(uris):
        return {"error": NO_IMAGES}
    if not (1 <= image <= len(uris) and uris[image - 1]):
        return {"error": f"Choose an image from 1 to {len(uris)}."}
    if await needs_confirmation("video_tryon"):
        open_capability("video_tryon")  # the user sees the page it would use
        return dict(NEEDS_CONFIRMATION)
    spec, _missing = job_spec("video_tryon")  # the image needs no input
    spec["tryon_image"] = uris[image - 1]
    return await _start(spec, "remake_video")


async def _start(spec: dict[str, Any], tool: str) -> dict[str, Any]:
    """Start ``spec`` through the cost guards (one job per response:
    ``start_once``) and follow it."""
    cap = spec["capability"]
    try:
        started = await start_once(_job_status, lambda: quota.start(spec))
    except GenMediaError as exc:  # refused by the cost guards, or storage
        logger.warning(
            "%s: %s", tool, exc, exc_info=not isinstance(exc, USER_ERRORS)
        )
        return {"error": user_message(exc, quota.NOT_STARTED)}
    if started is None:
        return {"error": quota.RUNNING}
    job = {"job_id": started["job_id"], "capability": cap}
    if spec.get("tryon_image"):  # a "Make video": its start clears nothing
        job["tryon_image"] = spec["tryon_image"]
    _st()["job"] = job  # saved with the studio, so a reopened panel follows it
    turn.patch("job_started", job)
    return {"status": "started", "job_id": started["job_id"], "capability": cap}


def _best(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The results for the model, best first: ref, score, view, verdict."""
    return [
        {
            "ref": r.get("uri", ""),
            "score": r.get("score"),
            "view": r.get("view", ""),
            "verdict": r.get("note", ""),
        }
        for r in results[:_MAX_RESULTS]
    ]


async def check_generation(wait_s: int = 0) -> dict[str, Any]:
    """How the studio's latest generation is going; once it has finished,
    its best results with their scores and verdicts.

    Args:
        wait_s: Seconds to wait for it to finish (at most 60); 0 answers at
            once.
    """
    deadline = time.monotonic() + max(0, min(wait_s, _MAX_WAIT_S))
    snap = await _job_status()
    while snap.get("status") == "running" and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_S)
        snap = await _job_status()
    if not snap:  # no job in the studio: the panel may have cleared it
        latest = turn.current().results
        if not latest.get("results"):
            return {"error": "No generation has been started in this studio."}
        snap = {"status": "done", **latest}
    out: dict[str, Any] = {
        k: snap[k]
        for k in ("job_id", "capability", "status", "progress", "elapsed_s")
        if k in snap
    }
    if snap.get("status") == "error":
        out["error"] = snap.get("error") or "The generation failed."
    if snap.get("status") == "done":
        out["results"] = _best(snap.get("results") or [])
    return out


TOOLS = [
    open_capability,
    find_product,
    choose_spin_set,
    use_image,
    choose_person,
    remove_product,
    clear,
    update_settings,
    generate,
    remake_video,
    check_generation,
]
