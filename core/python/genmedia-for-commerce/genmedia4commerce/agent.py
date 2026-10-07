# Copyright 2025 Google LLC
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

"""GenMedia ADK Agent.

Routes user requests to the appropriate media generation workflow
via MCP tools served by the GenMedia MCP server.

Uses before_model_callback to intercept user-uploaded images:
- Extracts base64 from inline image parts and stores in state
- Automatically injects into tool args (garment_images_base64, full_body_image_base64, etc.)

Uses after_tool_callback to intercept MCP tool responses:
- Extracts base64 images/videos and saves them as artifacts
- Returns a clean summary to the LLM (no base64 blobs in context)
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    SseConnectionParams,
    StdioConnectionParams,
)
from google.genai import types
from mcp import StdioServerParameters

from genmedia4commerce.agent_utils import (
    _download_from_gcs,
    append_to_history,
    copy_gcs_asset,
    describe_input_attachments_parallel,
    extract_media,
    generated_asset_path,
    resolve_filename_to_gcs_uri,
    resolve_media,
    retrieve_from_history,
    upload_asset_to_gcs,
    user_upload_path,
)
from genmedia4commerce.config import config
from genmedia4commerce.workflows.shared.llm_utils import (
    get_mime_type_from_bytes,
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
project_id = os.environ["GOOGLE_CLOUD_PROJECT"]


async def build_conversational_history(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """Before-model callback that handles both ADK and GE playground sessions.

    - Ensures a stable global session ID (persisted via GCS) for storing
      media and conversation history across requests, even when the GE
      playground assigns a new temp session ID on every turn.
    - Processes user-uploaded attachments for both ADK (inline_data in
      conversation parts) and GE (artifacts replayed from the request payload),
      uploading them to GCS and replacing raw bytes with GCS URIs.
    - Optimizes the conversation history sent to the LLM (e.g. stripping
      large binary payloads, ensuring clean context).
    """
    timestamp = str(int(time.time()))
    if "uploaded_images_base64" not in callback_context.state:
        callback_context.state["uploaded_images_base64"] = []

    # Retrieve uploads from artifact service (Agent Space style)
    invocation_context = callback_context._invocation_context
    session_id = invocation_context.session.id

    # Identify if global session id has been set before, if not flag it so that we can set it in the after model
    global_session_id = None
    for content in llm_request.contents:
        if content.role == "model":
            for part in content.parts or []:
                if part.text and "[session_id=" in part.text:
                    global_session_id = part.text.split("[session_id=")[
                        1
                    ].split("]")[0]
                    callback_context.state["is_global_session_id_set"] = True
                    break
    if global_session_id is None:
        global_session_id = session_id
        callback_context.state["is_global_session_id_set"] = False
    callback_context.state["global_session_id"] = global_session_id
    msg = (
        f"[before_model] global_session_id={global_session_id}, "
        f"current_session_id={session_id}, "
        f"is_global_session_id_set={callback_context.state['is_global_session_id_set']}"
    )
    logger.info(msg)

    # Collect all image attachments (ADK inline_data + GE artifacts)
    image_attachments = []  # list of (index_in_parts, img_bytes, mime_type, source)

    # Extract artifact filenames from GE markers in the last message
    last_parts = llm_request.contents[-1].parts
    ge_artifact_keys = []
    for part in last_parts:
        if part.text and "start_of_user_uploaded_file" in part.text:
            # Extract filename from "<start_of_user_uploaded_file: FILENAME>"
            match = re.search(
                r"start_of_user_uploaded_file:\s*(.+?)>", part.text
            )
            if match:
                ge_artifact_keys.append(match.group(1).strip())
    logger.debug(
        f"[before_model] GE artifact keys from message markers: {ge_artifact_keys}"
    )

    if ge_artifact_keys:
        # GE Case: load only the artifacts referenced in the current message
        for k in ge_artifact_keys:
            attachment = await callback_context.load_artifact(filename=k)
            msg = f"[before_model] Artifact '{k}': type={type(attachment).__name__}, has_inline_data={bool(attachment and attachment.inline_data)}, data_len={len(attachment.inline_data.data) if attachment and attachment.inline_data and attachment.inline_data.data else 0}"
            logger.info(msg)
            if (
                attachment
                and attachment.inline_data
                and attachment.inline_data.data
            ):
                img_bytes = attachment.inline_data.data
                mime_type = (
                    attachment.inline_data.mime_type
                    or get_mime_type_from_bytes(img_bytes)
                )
                image_attachments.append((None, img_bytes, mime_type, "ge"))

    # ADK Case: inline_data in the last user message
    else:
        parts = llm_request.contents[-1].parts
        logger.info(f"[before_model] Last user message has {len(parts)} parts")
        for index, part in enumerate(parts):
            has_inline = bool(part.inline_data and part.inline_data.data)
            msg = f"[before_model] Part {index}: text={bool(part.text)}, inline_data={has_inline}, text_preview={part.text[:100] if part.text else None}"
            logger.info(msg)
            if has_inline:
                image_attachments.append(
                    (
                        index,
                        part.inline_data.data,
                        part.inline_data.mime_type,
                        "adk",
                    )
                )

    msg = f"[before_model] Total image_attachments: {len(image_attachments)} (sources: {[att[3] for att in image_attachments]})"
    logger.info(msg)

    if image_attachments:
        # Describe all attachments in parallel
        # Build conversation context for image description
        context_lines = []
        for content in llm_request.contents[:-1]:
            for part in content.parts or []:
                if part.text:
                    context_lines.append(f"{content.role}: {part.text}")
        conversation_context = "\n".join(context_lines[-10:])
        loop = asyncio.get_running_loop()
        all_bytes = [att[1] for att in image_attachments]
        descriptions = await loop.run_in_executor(
            None,
            describe_input_attachments_parallel,
            all_bytes,
            conversation_context,
        )

        # Upload all to GCS in parallel
        upload_tasks = []
        filenames = []
        for i, (_, img_bytes, mime_type, _) in enumerate(image_attachments):
            ext = mime_type.split("/")[-1]
            filename = f"{timestamp}_u_{i}.{ext}"
            filenames.append(filename)
            upload_tasks.append(
                upload_asset_to_gcs(
                    data=img_bytes,
                    blob_path=user_upload_path(global_session_id, filename),
                    content_type=mime_type,
                )
            )
        await asyncio.gather(*upload_tasks)

        # Replace image parts (inline_data or GE markers) with text description + filename
        desc_idx = 0
        parts_modified = []
        for part in llm_request.contents[-1].parts:
            if part.inline_data and part.inline_data.data:
                parts_modified.append(
                    types.Part.from_text(
                        text=f"[user_upload | filename: {filenames[desc_idx]} | description: {descriptions[desc_idx]}]"
                    )
                )
                desc_idx += 1
            elif part.text and "start_of_user_uploaded_file" in part.text:
                parts_modified.append(
                    types.Part.from_text(
                        text=f"[user_upload | filename: {filenames[desc_idx]} | description: {descriptions[desc_idx]}]"
                    )
                )
                desc_idx += 1
            elif part.text and "end_of_user_uploaded_file" in part.text:
                pass
            else:
                parts_modified.append(part)
        llm_request.contents[-1].parts = parts_modified

    # Only append user messages — function_responses are already saved by handle_tool_response
    last_content = llm_request.contents[-1]
    is_tool_response = any(
        p.function_response for p in (last_content.parts or [])
    )
    if not is_tool_response:
        append_to_history(
            global_session_id=global_session_id, data=last_content
        )

    history = retrieve_from_history(global_session_id=global_session_id)
    if history:
        llm_request.contents = history
    else:
        # GCS history unavailable (e.g. media bucket missing): keep the
        # contents ADK built from the session rather than sending none.
        logger.warning(
            f"[before_model] Empty GCS history for {global_session_id}; "
            "falling back to session contents"
        )


async def inject_uploaded_images(
    tool: BaseTool, args: dict, tool_context: ToolContext
) -> dict | None:
    """Before-tool callback: resolve filenames to base64 data.

    The LLM passes filenames (from user uploads, catalog items, or generated assets).
    This callback reconstructs the GCS URI from the filename, downloads the image,
    and replaces the filename with base64-encoded data.
    """
    global_session_id = tool_context.state.get("global_session_id")

    loop = asyncio.get_running_loop()

    for param, val in list(args.items()):
        if not param.endswith("_base64"):
            continue

        if isinstance(val, str) and len(val) < 500:
            gcs_uri = resolve_filename_to_gcs_uri(val, global_session_id)
            logger.info(f"Resolving {param}: {val} → {gcs_uri}")
            logger.debug(f"[before_tool] Resolving {param}: {val} → {gcs_uri}")
            data = await loop.run_in_executor(None, _download_from_gcs, gcs_uri)
            if data:
                args[param] = base64.b64encode(data).decode("utf-8")

        elif isinstance(val, list):
            items_to_resolve = [
                (i, item)
                for i, item in enumerate(val)
                if isinstance(item, str) and len(item) < 500
            ]
            if not items_to_resolve:
                continue
            uris = [
                resolve_filename_to_gcs_uri(item, global_session_id)
                for _, item in items_to_resolve
            ]
            logger.info(f"Resolving {param}: {len(uris)} filenames")
            logger.debug(
                f"[before_tool] Resolving {param}: {len(uris)} filenames → {uris}"
            )
            downloads = await asyncio.gather(
                *[
                    loop.run_in_executor(None, _download_from_gcs, uri)
                    for uri in uris
                ]
            )
            for (i, _), data in zip(items_to_resolve, downloads, strict=True):
                if data:
                    val[i] = base64.b64encode(data).decode("utf-8")

    return None


async def handle_tool_response(
    tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: dict
) -> dict | None:
    """After-tool callback to intercept MCP tool results.

    Save media as artifacts, and persist a structured function_response to GCS history.

    - Catalog: FunctionResponse with description text + image URI per item via parts
    - Other tools: uploads generated media to GCS, FunctionResponse with URI parts
    """
    tool_name = tool.name
    result = tool_response
    logger.debug(
        f"[after_tool] ENTER tool={tool_name}, result_type={type(result).__name__}"
    )

    raw_text = None
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    raw_text = part.get("text", "")
                    break
        if raw_text is None:
            raw_text = json.dumps(result)

    if raw_text is None:
        logger.debug(
            f"[after_tool] EARLY EXIT: raw_text is None for {tool_name}"
        )
        return None

    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        logger.debug(
            f"[after_tool] EARLY EXIT: JSON parse failed for {tool_name}, raw_text[:200]={raw_text[:200]}"
        )
        return None

    global_session_id = tool_context.state.get("global_session_id")
    timestamp = str(int(time.time()))
    logger.debug(
        f"[after_tool] tool={tool_name}, global_session_id={global_session_id}"
    )
    logger.debug(
        f"[after_tool] parsed type={type(parsed).__name__}, keys={list(parsed.keys()) if isinstance(parsed, dict) else 'N/A'}"
    )

    if tool_name == "catalog_search":
        items = parsed if isinstance(parsed, list) else []
        logger.debug(f"[after_tool] catalog_search: {len(items)} items")

        # Download catalog images individually to maintain 1:1 index correspondence.
        # resolve_media drops failed downloads which breaks positional indexing.
        loop = asyncio.get_running_loop()
        download_results = await asyncio.gather(
            *[
                loop.run_in_executor(
                    None, _download_from_gcs, item.get("img_path", "")
                )
                if item.get("img_path")
                else asyncio.sleep(0)
                for item in items
            ]
        )
        logger.debug("[after_tool] catalog_search: downloads complete")

        item_lines = []
        copy_tasks = []
        artifact_tasks = []
        for i, item in enumerate(items):
            description = item.get("description", "")
            img_path = item.get("img_path", "")
            orig_filename = img_path.rsplit("/", 1)[-1] if img_path else ""
            ext = (
                orig_filename.rsplit(".", 1)[-1]
                if "." in orig_filename
                else "jpg"
            )
            filename = f"{timestamp}_c_{i}.{ext}"
            item_lines.append(
                f"item_id: {filename}\ndescription: {description}"
            )
            # Copy to session generated_assets (server-side, no download)
            if img_path and global_session_id:
                copy_tasks.append(
                    copy_gcs_asset(
                        source_uri=img_path,
                        dest_blob_path=generated_asset_path(
                            global_session_id, filename
                        ),
                    )
                )
            # Save as ADK artifact for frontend visualization
            img_data = download_results[i]
            if isinstance(img_data, bytes):
                mime = get_mime_type_from_bytes(img_data)
                part = types.Part(
                    inline_data=types.Blob(mime_type=mime, data=img_data)
                )
                artifact_tasks.append(
                    tool_context.save_artifact(filename=filename, artifact=part)
                )
        logger.debug(
            f"[after_tool] catalog_search: {len(copy_tasks)} copy_tasks, "
            f"{len(artifact_tasks)} artifact_tasks"
        )
        await asyncio.gather(*copy_tasks, *artifact_tasks)
        logger.debug("[after_tool] catalog_search: done saving artifacts")
        tool_result = {"output": "\n\n".join(item_lines)}
    else:
        # Extract media (base64 → bytes, gs:// URIs → str) and resolve
        media = extract_media(parsed, tool_name)
        logger.debug(
            f"[after_tool] {tool_name}: extracted {len(media)} media items (types: {[type(m).__name__ for m in media]})"
        )
        resolved = await resolve_media(media) if media else []
        logger.debug(
            f"[after_tool] {tool_name}: resolved {len(resolved)} media items"
        )
        # Upload generated media to GCS + save as artifacts in parallel
        upload_tasks = []
        artifact_tasks = []
        filenames = []
        for i, data in enumerate(resolved):
            mime = get_mime_type_from_bytes(data)
            ext = mime.split("/")[-1]
            filename = f"{timestamp}_g_{i}.{ext}"
            filenames.append(filename)
            upload_tasks.append(
                upload_asset_to_gcs(
                    data=data,
                    blob_path=generated_asset_path(global_session_id, filename),
                    content_type=mime,
                )
            )
            part = types.Part(inline_data=types.Blob(mime_type=mime, data=data))
            artifact_tasks.append(
                tool_context.save_artifact(filename=filename, artifact=part)
            )
        logger.debug(
            f"[after_tool] {tool_name}: {len(upload_tasks)} upload_tasks, {len(artifact_tasks)} artifact_tasks"
        )
        await asyncio.gather(*upload_tasks, *artifact_tasks)
        logger.debug(
            f"[after_tool] {tool_name}: done saving artifacts, filenames={filenames}"
        )
        tool_result = {
            "output": f"Generated {len(filenames)} asset(s): {', '.join(filenames)}"
        }

    # Save function_response to GCS history

    fn_response = types.FunctionResponse(
        name=tool_name,
        response=tool_result,
    )
    tool_response_content = types.Content(
        role="user",
        parts=[types.Part(function_response=fn_response)],
    )
    append_to_history(global_session_id, tool_response_content)

    return tool_result


def after_model_init_global_session_id_and_store_model_response(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """After-model callback: inject a global session ID into the first model response.

    On the GE playground, session IDs are random and change every request,
    making them unusable for persistent storage keys. This callback embeds
    a stable UUID (the global session ID) into the first model text response
    as a `[session_id=...]` prefix. Because GE replays prior conversation
    turns, this prefix reappears in subsequent requests, allowing
    before_model_callback to extract it and use it as a consistent key
    for GCS-based conversation history and media storage.

    Only runs once per conversation (skips if the ID has already been set).
    Also appends the model response to the GCS-backed conversation history.
    """
    is_global_session_id_set = callback_context.state[
        "is_global_session_id_set"
    ]
    global_session_id = callback_context.state["global_session_id"]

    msg = f"[after_model] global_session_id={global_session_id}, is_global_session_id_set={is_global_session_id_set}"
    logger.info(msg)

    if not is_global_session_id_set:
        # First turn: inject session ID prefix into the first text part
        is_first_text = True
        if llm_response.content and llm_response.content.parts:
            parts = llm_response.content.parts
            parts_modified = []
            for part in parts:
                if part.text:
                    if len(part.text.strip()) > 0 and is_first_text:
                        part.text = (
                            f"[session_id={global_session_id}]\n\n{part.text}"
                        )
                        parts_modified.append(part)
                        is_first_text = False
                    else:
                        parts_modified.append(part)
                else:
                    parts_modified.append(part)
            llm_response.content.parts = parts_modified

    append_to_history(global_session_id=global_session_id, data=llm_response)


root_agent = Agent(
    name="genmedia_router",
    model=Gemini(
        model=config.agent_model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are GenMedia for Commerce — an AI assistant that helps brands and retailers
create professional marketing visuals powered by generative AI.

You can generate virtual try-on images and videos, 360-degree product spinning videos,
product fitting on models, and more. If the user doesn't have product images handy,
offer to search the product catalogue for them.

## What You Can Do

**Virtual try-on (image)** — Generate realistic images of a person wearing garments or glasses.
Upload a photo of the person and the product to get a polished try-on result.
Tool: `image_vto` — set `is_glasses=True` for eyewear, `False` for clothing.
IMPORTANT: When the user wants to see a complete outfit, pass ALL garment/product filenames together
in `product_images_base64` — e.g. if the user uploaded shoes and searched for matching trousers,
include both the shoes and the selected trousers in the list so the model wears the full outfit.

**Virtual try-on (video)** — Same concept, but produces a short video.
Tool: `video_vto` — set `is_glasses=True` for eyewear, `False` for clothing.

**Animate model** — Already have a model image (e.g. from a previous image VTO result)?
Skip image generation and go straight to video. Tool: `animate_model`.
Tip: after a successful `image_vto`, offer to animate the result into a video using this tool.

**Product fitting on a model** — No model photo? Provide garment images and a gender,
and the system generates front and back views on a virtual model body.
Tool: `product_fitting`

**360-degree spinning video** — Create a spinning product video from a few angle shots.
Works with shoes, bags, headphones, mugs — any product.
Tool: `product_spinning` — set `is_shoes=True` for shoes (specialized pipeline), `False` for anything else.

**Change background** — Swap the background of a person photo while keeping the person intact.
Tool: `background_changer`

**Browse the catalogue** — Search products by description.
Tool: `catalog_search` — always search with `k=1` (single result).
Use the `audience` filter only when the user specifies a gender. Allowed values: "men", "women",
"unisex", or comma-separated (e.g. "men,unisex"). Do NOT pass `audience` if the user doesn't specify one.

**Catalogue search flow:**
1. **Clarify first**: Before searching, make sure you know the audience and style.
   If the user just says "find me jeans", check context (e.g. they uploaded a photo of themselves
   as a woman → audience is clear). If not clear, ask: "Are you looking for women's, men's, or unisex?"
   Also ask about style preferences if not obvious (casual, formal, streetwear, etc.).
2. **Craft a rich query**: Don't just search "jeans". Add flavour based on context — e.g.
   "slim fit dark indigo jeans with a modern tapered leg" or "relaxed wide-leg light wash jeans
   with vintage feel". Use your fashion knowledge to make the query specific and interesting.
3. **Present results**: Show ONE result at a time. Write a short, refined one-liner description
   of the product focusing on its key features (e.g. "Slim-fit dark indigo jeans with a tapered
   leg and subtle whiskering"). Do NOT include metadata like season, audience, or the item_id.
   Do NOT be verbose — just the one-liner.
4. **Iterate**: Ask if they like it or want to see alternatives. Offer 1-3 variation directions
   (e.g. "1. lighter wash  2. more relaxed fit  3. distressed style"). The user picks one and
   you search again with an adjusted query. Repeat until they're happy.

When the user asks to find items that "match" or "go well with" an existing item, use your fashion
knowledge to craft specific queries. Consider the item's color, style, and occasion to suggest
complementary pieces.

## How Images Work

All filenames follow the pattern: TIMESTAMP_TYPE_INDEX.EXT
- _u_ = user upload (e.g. 1719000000_u_0.jpg)
- _c_ = catalog item (e.g. 1719000000_c_2.jpg)
- _g_ = generated asset (e.g. 1719000000_g_0.png)

When users upload images, each is described and assigned a filename.
You will see them as: [user_upload | filename: 1719000000_u_0.jpg | description: ...]

When catalogue search returns results, each item has an item_id (filename) and description.

When calling generation tools, pass the **filename** for any `_base64` parameter.
The system resolves filenames to actual image data automatically.

You can also pass filenames from previous tool results as inputs to new tools.
For example, after image_vto returns 1719000000_g_0.png, you can use it as:
- person_image_base64 for another image_vto (e.g. to change shoes on the result)
- model_image_base64 for animate_model (to animate the VTO result)
- person_image_base64 for background_changer (to change the background)

Example:
- User uploads → [user_upload | filename: 1719000000_u_0.jpg | description: woman in blue dress]
- Catalog item → item_id: 1719000000_c_0.jpg
- VTO call: image_vto(person_image_base64="1719000000_u_0.jpg", product_images_base64=["1719000000_c_0.jpg"])
- Chain: image_vto(person_image_base64="1719000000_g_0.png", product_images_base64=["1719000000_c_2.jpg"])

## Tool Reference

### image_vto
- person_image_base64: person image (full body for clothes, face for glasses)
- product_images_base64: product images list
- is_glasses: True for eyewear, False for clothing (default)
- scenario, num_variations, face_image_base64: optional

### video_vto
- person_image_base64, product_images_base64, is_glasses: same as image_vto
- number_of_videos (default: 4), prompt: optional
- scenario, num_variations, face_image_base64: optional (clothes only)

### animate_model
- model_image_base64: image of model already wearing garments (e.g. from image_vto result)
- number_of_videos (default: 4), prompt: optional
- Use this instead of video_vto when the image is already ready — skips image generation

### product_fitting
- garment_images_base64: garment images list
- gender: man or woman

### product_spinning
- images_base64: product images from multiple angles
- is_shoes: True for shoes, False for other products (default)

### background_changer
- person_image_base64: person image
- background_description or background_image_base64: describe or provide reference

### catalog_search
- query: rich text description (e.g. "slim fit dark indigo jeans with tapered leg")
- k: always use 1
- audience: optional — "women", "men", "unisex", or comma-separated. Omit if not specified.

## Guidelines

1. **Be proactive after uploads**: When the user uploads images, acknowledge what you received
   (using the descriptions), then suggest next steps based on context:
   - If they uploaded a person photo only: ask if they want to upload garment/product images, or search the catalogue.
   - If they uploaded garments only: ask if they want to upload a person photo, use product fitting (no person needed), or search the catalogue.
   - If they uploaded both a person and garments: suggest running image VTO, video VTO, or ask if they want to upload more.
   - If they uploaded a product: suggest spinning video, background change, or product fitting.
   Always list the relevant options so the user knows what's possible.
2. For VTO requests, ask whether they want an image or a video if it's not clear.
3. Report results clearly — mention quality scores and that media has been saved.
4. If a tool returns an error, explain it and suggest what the user can try.
5. NEVER retry a tool unless the user explicitly asks.
6. For catalogue results, images are automatically saved as artifacts and displayed
   by the frontend. Do NOT include image URLs or <img> tags in your response.
7. NEVER include session IDs, GCS URIs, or internal system identifiers in your responses to the user.
   You may see `[session_id=...]` prefixed in your earlier messages — that is injected by the system
   and must NOT be repeated or included in any of your responses.
8. **Matching images by description**: When the user refers to an image by its visual attributes
   (e.g. "the red t-shirt with the logo", "the woman in the blue dress"), match their description
   against the detailed descriptions of uploaded images and catalogue results to find the correct filename.
   Each upload and catalogue item has a unique filename — always use that exact filename in tool calls.
""",
    tools=[
        McpToolset(
            connection_params=SseConnectionParams(
                url=config.mcp_server_url,
                timeout=30,
                sse_read_timeout=600,
            )
            if os.getenv("MCP_SERVER_URL")
            else StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "genmedia4commerce.mcp_server.server"],
                ),
                timeout=600,
            ),
        )
    ],
    before_model_callback=build_conversational_history,
    after_model_callback=after_model_init_global_session_id_and_store_model_response,
    before_tool_callback=inject_uploaded_images,
    after_tool_callback=handle_tool_response,
)

app = App(
    root_agent=root_agent,
    name="genmedia4commerce",
)
