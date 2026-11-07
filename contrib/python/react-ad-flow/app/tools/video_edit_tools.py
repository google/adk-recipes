"""Post-generation visual video editing via Gemini Omni Flash's Interactions
API (Phase 14c — experimental, opt-in, ENABLE_OMNI_EDIT-gated).

Two live-discovered API contract details, undocumented by Google, that this
module must always honor:
  1. VideoContent.data must be a pathlib.Path (or IO[bytes]) to be
     auto-base64-encoded by the SDK -- a bare str is sent as literal
     (corrupt) "base64 data" and only fails server-side with a 400.
  2. response_format must never set aspect_ratio for an edit-task call --
     the server derives it from the input video and 400s if set explicitly.

Both video editing (this module) and video generation (video_tools.py) use
Gemini Omni Flash via the Interactions API (client.interactions).
"""

import asyncio
import base64
import binascii
import logging
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import interactions as im

from .. import storage
from ..config import OMNI_EDIT_MODEL
from ..database.db import get_db_cursor

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "incomplete",
    "budget_exceeded",
}
_POLL_INTERVAL_SECONDS = 10


class OmniEditError(Exception):
    """Base error for Omni Flash edit-tool failures."""


def _load_video_bytes_for_edit(video_id: int) -> tuple[bytes, dict]:
    """Resolve a campaign_videos row to (video_bytes, row_dict).

    Reuses storage.read_video(), which already handles local-vs-GCS storage
    mode -- this function does not need its own branching for that.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM campaign_videos WHERE id = ?", (video_id,)
        )
        row = cursor.fetchone()
    if not row:
        raise OmniEditError(f"Video {video_id} not found")
    row_dict = dict(row)
    video_bytes = storage.read_video(row_dict["video_filename"])
    return video_bytes, row_dict


def _build_client() -> genai.Client:
    return (
        genai.Client()
    )  # picks up GOOGLE_GENAI_USE_VERTEXAI/PROJECT/LOCATION from env


def _extract_video_bytes(interaction) -> bytes:
    """Pull the model_output step's video content and base64-decode it."""
    try:
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for content in getattr(step, "content", None) or []:
                if getattr(content, "type", None) == "video":
                    data_b64 = getattr(content, "data", None)
                    if data_b64:
                        return base64.b64decode(data_b64, validate=True)
                    uri = getattr(content, "uri", None)
                    if uri:
                        raise OmniEditError(
                            f"Video delivered as uri, not inline: {uri}"
                        )
        output_video = getattr(interaction, "output_video", None)
        if output_video:
            data_b64 = getattr(output_video, "data", None)
            if data_b64:
                return base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise OmniEditError(
            f"Omni Flash returned undecodable video data: {e}"
        ) from e
    raise OmniEditError(
        "Omni Flash response completed but contained no video content"
    )


async def _wait_for_omni_interaction(client, interaction, max_wait_time: int):
    """Poll a background interaction to a terminal status."""
    waited = 0
    status = getattr(interaction, "status", None)
    while status not in _TERMINAL_STATUSES:
        if waited >= max_wait_time:
            raise TimeoutError(
                f"Omni Flash edit timed out after {max_wait_time}s (last status={status})"
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
        interaction = client.interactions.get(interaction.id)
        status = getattr(interaction, "status", None)
    return interaction


async def edit_video_with_omni(
    video_id: int,
    edit_instruction: str,
    *,
    max_wait_time: int = 300,
) -> dict:
    """Apply a targeted visual edit to an existing generated video via
    Gemini Omni Flash's Interactions API (video_config.task="edit").

    Verified working for single-attribute visual edits (e.g. "change the
    shirt color to red"). NOT for audio/music/dialogue/language changes --
    Omni Flash does not support voice editing (confirmed broken; see the
    phase doc). NOT for multi-attribute instructions or chained/iterative
    revision (previous_interaction_id) -- unverified, do not rely on it.

    Args:
        video_id: DB id of a previously generated video (campaign_videos.id).
        edit_instruction: natural-language, single-attribute edit request.
        max_wait_time: seconds to wait for the background interaction to
            reach a terminal status.

    Returns:
        On success: {"success": True, "video_id": <new id>,
            "source_video_id": video_id, "video_filename": ...,
            "backend": "omni_flash", "interaction_id": ..., "duration_seconds": ...}
        On failure: {"success": False, "error": <message>,
            "error_type": "not_found" | "storage_error" | "unsupported_edit"
                | "timeout" | "api_error"}
    """
    try:
        video_bytes, row = _load_video_bytes_for_edit(video_id)
    except OmniEditError as e:
        return {"success": False, "error": str(e), "error_type": "not_found"}
    except Exception as e:
        logger.exception(
            "Failed to load source video bytes for video_id=%s", video_id
        )
        return {
            "success": False,
            "error": str(e),
            "error_type": "storage_error",
        }

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = Path(tmp.name)

    try:
        try:
            client = _build_client()
            user_step = im.UserInputStep(
                content=[
                    im.VideoContent(data=tmp_path, mime_type="video/mp4"),
                    im.TextContent(text=edit_instruction),
                ]
            )
            generation_config = im.GenerationConfig(
                video_config=im.VideoConfig(task="edit")
            )
            response_format = im.VideoResponseFormat(
                type="video", delivery="inline"
            )
            interaction = client.interactions.create(
                model=OMNI_EDIT_MODEL,
                input=[user_step],
                generation_config=generation_config,
                response_format=response_format,
                background=True,
            )
            interaction = await _wait_for_omni_interaction(
                client, interaction, max_wait_time
            )
        except TimeoutError as e:
            return {"success": False, "error": str(e), "error_type": "timeout"}
        except Exception as e:
            logger.exception(
                "Omni Flash edit call failed for video_id=%s", video_id
            )
            return {
                "success": False,
                "error": str(e),
                "error_type": "api_error",
            }

        if interaction.status not in ("completed",):
            if interaction.status in ("failed", "incomplete"):
                error_type = "unsupported_edit"
            else:
                # cancelled, budget_exceeded: not a content/support problem --
                # don't mislead the agent into telling the user "unsupported."
                error_type = "api_error"
            return {
                "success": False,
                "error": f"Omni Flash interaction ended with status={interaction.status}",
                "error_type": error_type,
            }

        try:
            edited_bytes = _extract_video_bytes(interaction)
        except OmniEditError as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": "api_error",
            }
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        new_filename = f"omni-edit-{int(time.time() * 1000)}.mp4"
        local_path = storage.save_video(new_filename, edited_bytes)

        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO campaign_videos
                    (campaign_id, product_id, video_filename, local_path, status,
                     duration_seconds, aspect_ratio, source_video_id,
                     scene_prompt, video_prompt, pipeline_type)
                VALUES (?, ?, ?, ?, 'generated', ?, ?, ?, ?, ?, 'omni-edit')
                """,
                (
                    row["campaign_id"],
                    row["product_id"],
                    new_filename,
                    local_path,
                    row["duration_seconds"],
                    row["aspect_ratio"],
                    video_id,
                    row.get("scene_prompt"),
                    edit_instruction,
                ),
            )
            new_video_id = cursor.lastrowid
    except Exception as e:
        logger.exception(
            "Failed to persist Omni-edited video for source_video_id=%s",
            video_id,
        )
        return {
            "success": False,
            "error": str(e),
            "error_type": "storage_error",
        }

    logger.info(
        "omni edit: source_video_id=%s new_video_id=%s interaction_id=%s",
        video_id,
        new_video_id,
        interaction.id,
    )

    return {
        "success": True,
        "video_id": new_video_id,
        "source_video_id": video_id,
        "video_filename": new_filename,
        "backend": "omni_flash",
        "interaction_id": interaction.id,
        "duration_seconds": row["duration_seconds"],
    }


async def translate_video_dialogue(video_id: int, target_language: str) -> dict:
    """NOT IMPLEMENTED. Documented placeholder for a researched-and-rejected
    capability. Gemini Omni Flash does not support voice editing/dubbing
    on any surface; a live test produced a partial, code-switched
    translation with unsynced lip movement. Do not wire this into any agent.
    If ever implemented, it must target a different model/product, with its
    own research spike first.
    """
    raise NotImplementedError(
        "Dialogue translation/dubbing is not supported by Gemini Omni Flash. "
        "This function is a documented placeholder, not a real backend."
    )
