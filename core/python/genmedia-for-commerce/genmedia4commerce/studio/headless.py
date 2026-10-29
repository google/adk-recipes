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

"""The Studio agent without a panel (``adk web``, the ADK API, ``adk eval``).

Attached images become Studio uploads (``[studio]`` lines, no bytes in the
session) and finished results are saved as ADK artifacts so the chat shows
them.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import logging
from dataclasses import dataclass
from typing import Any

from google.adk.tools import ToolContext
from google.genai import types

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import USER_ERRORS
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import turn

__all__ = ["Headless", "save_artifacts", "without_attachments"]

logger = logging.getLogger(__name__)

#: Session state key: the job whose results were saved as artifacts.
_ARTIFACTS_OF = "studio_artifacts_of"


@dataclass
class Headless(turn.Scope):
    """The scope of a turn ADK started itself: no panel shows the studio."""

    token: contextvars.Token | None = None  # set by _before_agent


async def _attachment_note(studio_id: str, blob: types.Blob) -> str:
    """Save an attached image as a studio upload; the ``[studio]`` line."""
    name = blob.display_name or "an image"
    if not (blob.mime_type or "").startswith("image/") or not blob.data:
        return f"The user attached {name} ({blob.mime_type}), not an image."
    data = base64.b64encode(blob.data).decode()
    try:
        up = await asyncio.to_thread(sm.save_upload, data, name, studio_id)
    except USER_ERRORS as exc:
        return f"The user attached {name}, which cannot be used: {exc}"
    except Exception:  # storage errors can name buckets: logged only
        logger.exception("Could not save an attached image")
        return f"The user attached {name}, which could not be saved."
    desc = up.get("description") or "no description"
    return f"The user attached {up['label']}: {desc}. ref: {up['ref']}"


async def without_attachments(
    studio_id: str, content: types.Content
) -> types.Content:
    """``content`` with each attached file replaced by a ``[studio]`` line."""
    parts = content.parts or []
    if not any(p.inline_data for p in parts):
        return content
    out = []
    for p in parts:
        if p.inline_data is None:
            out.append(p)
            continue
        note = await _attachment_note(studio_id, p.inline_data)
        out.append(types.Part(text=sh.STUDIO_PREFIX + note))
    return types.Content(role=content.role, parts=out)


async def save_artifacts(
    tool_context: ToolContext, response: dict[str, Any]
) -> None:
    """Save a finished generation's results as ADK artifacts, once per job,
    so ``adk web`` shows them."""
    job = str(response.get("job_id") or "latest")
    if tool_context.state.get(_ARTIFACTS_OF) == job:
        return
    for r in response.get("results") or []:
        ref = str(r.get("ref") or "")
        try:
            data = await asyncio.to_thread(gcs.download, ref)
            part = types.Part.from_bytes(data=data, mime_type=sm.mime_of(ref))
            name = f"{job}-{ref.rsplit('/', 1)[-1]}"
            await tool_context.save_artifact(name, part)
        except Exception as exc:
            logger.warning("Could not save %s as an artifact: %s", ref, exc)
    tool_context.state[_ARTIFACTS_OF] = job
