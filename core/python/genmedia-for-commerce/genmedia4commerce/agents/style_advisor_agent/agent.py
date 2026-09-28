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

"""Fashion Curation System.

Composed of:
1. Style Advisor (Searcher): Handles retrieval and catalog queries.
2. Stylish Agent (Judge): Curates search results into 3 high-quality looks.
"""

import base64
import hashlib
import logging
import os
import re
from datetime import timedelta

import google.auth
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.google_search_tool import google_search
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams
from google.cloud import storage
from google.genai import types

logger = logging.getLogger(__name__)

try:
    _, project_id = google.auth.default()
except google.auth.exceptions.DefaultCredentialsError:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
os.environ.setdefault(
    "GOOGLE_CLOUD_LOCATION", os.getenv("GLOBAL_REGION", "global")
)
# This agent is Vertex AI only; set explicitly so a stale env value cannot
# silently override the detected auth mode.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8081/sse")


# --- Shared Callbacks & Logic (Mirrors router_agent) ---


async def extract_uploaded_images(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """Extract uploaded images from the LLM request and store them in state."""
    if "uploaded_images_base64" not in callback_context.state:
        callback_context.state["uploaded_images_base64"] = []
    images_b64 = []
    if llm_request.contents:
        last_user_content = None
        for content in llm_request.contents:
            if content.role == "user":
                last_user_content = content
        if last_user_content and last_user_content.parts:
            for part in last_user_content.parts:
                if part.inline_data and part.inline_data.data:
                    img_bytes = part.inline_data.data
                    if isinstance(img_bytes, bytes):
                        images_b64.append(
                            base64.b64encode(img_bytes).decode("utf-8")
                        )
                    elif isinstance(img_bytes, str):
                        images_b64.append(img_bytes)
    if images_b64:
        callback_context.state["uploaded_images_base64"] = images_b64
        index_note = (
            f"[System: {len(images_b64)} images uploaded, indexed as: "
            + ", ".join(f"image {i}" for i in range(len(images_b64)))
            + ". Use these indices when calling tools.]"
        )
        llm_request.contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=index_note)]
            )
        )


async def inject_uploaded_images(
    tool: BaseTool, args: dict, tool_context: ToolContext
) -> dict | None:
    """Inject uploaded images into tool arguments."""
    uploaded = tool_context.state.get("uploaded_images_base64", [])

    def _resolve_index(val: str) -> str | None:
        try:
            idx = int(val)
            if 0 <= idx < len(uploaded):
                return uploaded[idx]
        except (ValueError, TypeError):
            pass
        return None

    async def _resolve_artifact(val: str) -> str | None:
        if not (
            val.endswith(".png") or val.endswith(".jpg") or val.endswith(".mp4")
        ):
            return None
        try:
            part = await tool_context.load_artifact(val)
            if part and part.inline_data and part.inline_data.data:
                data = part.inline_data.data
                if isinstance(data, bytes):
                    return base64.b64encode(data).decode("utf-8")
                return data
        except Exception:  # noqa: S110
            pass
        return None

    def _resolve_url(val: str) -> str | None:
        if val.startswith("gs://"):
            return val
        for prefix in [
            "https://storage.cloud.google.com/",
            "https://storage.googleapis.com/",
        ]:
            if val.startswith(prefix):
                return "gs://" + val[len(prefix) :].split("?", maxsplit=1)[0]
        if not val.startswith(("http://", "https://")):
            return None
        try:
            import httpx

            resp = httpx.get(val, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode("utf-8")
        except Exception:
            return None

    async def _resolve(val: str) -> str | None:
        return (
            _resolve_index(val)
            or await _resolve_artifact(val)
            or _resolve_url(val)
        )

    for param, val in list(args.items()):
        if not param.endswith("_base64"):
            continue
        if isinstance(val, str) and len(val) < 1000:
            res = await _resolve(val)
            if res:
                args[param] = res
        elif isinstance(val, list):
            args[param] = [
                (
                    await _resolve(i)
                    if isinstance(i, str) and len(i) < 1000
                    else i
                )
                for i in val
            ]
    return None


def _is_b64_blob(v) -> bool:
    return isinstance(v, str) and len(v) > 1000


def _strip_images_from_result(
    result: dict, tool_name: str
) -> tuple[dict, dict[str, bytes]]:
    artifacts: dict[str, bytes] = {}

    def _ext(k):
        return ".mp4" if "video" in k else ".png"

    def _clean_dict(d: dict, prefix: str) -> dict:
        clean = {}
        for k, v in d.items():
            if k.endswith("_base64"):
                if isinstance(v, list):
                    cl = []
                    for i, item in enumerate(v):
                        if _is_b64_blob(item):
                            name = f"{prefix}{k.replace('_base64', '')}_{i}{_ext(k)}"
                            artifacts[name] = base64.b64decode(item)
                            cl.append(name)
                        else:
                            cl.append(item)
                    clean[k.replace("_base64", "_artifact")] = cl
                elif _is_b64_blob(v):
                    name = f"{prefix}{k.replace('_base64', '')}{_ext(k)}"
                    artifacts[name] = base64.b64decode(v)
                    clean[k.replace("_base64", "_artifact")] = name
                else:
                    clean[k] = v
            elif isinstance(v, dict):
                clean[k] = _clean_dict(v, f"{prefix}{k}_")
            elif isinstance(v, list):
                clean[k] = [
                    (
                        _clean_dict(i, f"{prefix}{k}_{idx}_")
                        if isinstance(i, dict)
                        else i
                    )
                    for idx, i in enumerate(v)
                ]
            else:
                clean[k] = v
        return clean

    return _clean_dict(result, f"{tool_name}_"), artifacts


def _generate_signed_url(url: str) -> str | None:
    try:
        if not (
            url.startswith("gs://")
            or "storage.cloud.google.com" in url
            or "storage.googleapis.com" in url
        ):
            return None
        client = storage.Client()
        path = (
            url.replace("gs://", "")
            .replace("https://storage.cloud.google.com/", "")
            .replace("https://storage.googleapis.com/", "")
            .split("?")[0]
        )
        parts = path.split("/", 1)
        if len(parts) != 2:
            return None
        blob = client.bucket(parts[0]).blob(parts[1])
        try:
            return blob.generate_signed_url(
                version="v4", expiration=timedelta(hours=1), method="GET"
            )
        except Exception:
            return None
    except Exception:
        return None


async def _fetch_image_data(url: str) -> tuple[bytes, str] | tuple[None, None]:
    try:
        client = storage.Client()
        data, mime = None, "image/png"
        if (
            url.startswith("gs://")
            or "storage.cloud.google.com" in url
            or "storage.googleapis.com" in url
        ):
            path = (
                url.replace("gs://", "")
                .replace("https://storage.cloud.google.com/", "")
                .replace("https://storage.googleapis.com/", "")
                .split("?")[0]
            )
            parts = path.split("/", 1)
            blob = client.bucket(parts[0]).blob(parts[1])
            data = blob.download_as_bytes()
            mime = blob.content_type or "image/png"
        elif url.startswith("http"):
            import httpx

            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(url, follow_redirects=True)
                data, mime = (
                    r.content,
                    r.headers.get("content-type", "image/png"),
                )
        return data, mime
    except Exception:
        return None, None


async def process_text_for_gcs_urls(
    text: str, runner_or_context, user_id="test_user", session_id="test_session"
) -> str:
    """Replace GCS URLs in a string with Proxy or Signed URLs."""
    if not text or not ("storage" in text or "gs://" in text):
        return text

    urls = re.findall(r'(https?://storage\.[^\s"\'\)]+|gs://[^\s"\'\)]+)', text)
    for url in list(set(urls))[:15]:
        final_url = _generate_signed_url(url)
        if not final_url:
            data, mime = await _fetch_image_data(url)
            if data:
                fname = f"item_{hashlib.md5(url.encode()).hexdigest()[:8]}.png"  # noqa: S324
                # Handle both Runner and ToolContext
                if hasattr(runner_or_context, "save_artifact"):
                    await runner_or_context.save_artifact(
                        fname,
                        types.Part(
                            inline_data=types.Blob(mime_type=mime, data=data)
                        ),
                    )
                elif hasattr(runner_or_context, "artifact_service"):
                    await runner_or_context.artifact_service.save_artifact(
                        app_name="style_advisor",
                        user_id=user_id,
                        session_id=session_id,
                        filename=fname,
                        artifact=types.Part(
                            inline_data=types.Blob(mime_type=mime, data=data)
                        ),
                    )
                final_url = f"/api/artifacts/{fname}?app=style_advisor&user={user_id}&session={session_id}"
        if final_url:
            text = text.replace(url, final_url)
    return text


async def handle_tool_response(
    tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: dict
) -> dict | None:
    """Handle tool response by stripping large base64 blobs and saving them as artifacts."""
    if tool_response is None:
        return None

    if isinstance(tool_response, dict):
        # Base64 stripping
        try:
            clean, artifacts = _strip_images_from_result(
                tool_response, tool.name
            )
            for name, data in artifacts.items():
                mime = "video/mp4" if name.endswith(".mp4") else "image/png"
                await tool_context.save_artifact(
                    name,
                    types.Part(
                        inline_data=types.Blob(mime_type=mime, data=data)
                    ),
                )
            if artifacts:
                clean["_note"] = (
                    f"Media saved as artifacts: {list(artifacts.keys())}"
                )
                return clean
        except Exception:  # noqa: S110
            pass

    return tool_response


# --- Stylish Agent (The Curator) ---

stylish_agent = Agent(
    name="stylish_agent",
    model=Gemini(model=os.getenv("MODEL_NAME_GENERATED_4", "gemini-3.6-flash")),
    instruction="""You are the Stylish Agent — a High-Fashion Curator.
Assemble 3 complete, premium looks from the candidates.

## Workflow
1. **EVALUATE** descriptions and images.
2. **HARMONIZE** 3 complete outfits (Top, Bottom, Shoes, Accessory).
3. **PRESENT** each look elegantly.

## Format Requirements
For each look, provide a cohesive presentation using clean spacing:

### ✨ Look [N]: [Inspiring Name of the Look]
*Provide a brief narrative on why these pieces harmonize beautifully.*

**The Ensemble:**
- **Top**: [Item Name]
- **Bottom**: [Item Name]
- **Footwear**: [Item Name]
- **Accessory**: [Item Name]

![[Item Name]](image_url)
""",
    before_model_callback=extract_uploaded_images,
    before_tool_callback=inject_uploaded_images,
    after_tool_callback=handle_tool_response,
)


# --- Style Advisor Agent (The Searcher) ---

style_advisor_agent = Agent(
    name="style_advisor",
    model=Gemini(model=os.getenv("MODEL_NAME_GENERATED_4", "gemini-3.6-flash")),
    instruction="""You are the Style Advisor. Find garments and delegate to the stylist.

## Workflow
1. **SEARCH** — Call `catalog_search(query=..., k=3)` for each required category. You MUST explicitly set k=3.
2. **STRICT SILENCE** — Do NOT show results, descriptions, IDs, or images to the user.
3. **DELEGATE** — Immediately hand off all findings (names, brands, prices, image URLs) to the `stylish_agent`.
4. **RESPONSE**: "I've found some great candidates! Passing them to our fashion curator to design 3 unique looks for you."

## Guidelines
- Do NOT curated looks yourself.
- Do NOT use Markdown/HTML images in your own response.
- If you call a tool, your next message MUST be the delegation to `stylish_agent`.
""",
    tools=[
        McpToolset(
            connection_params=SseConnectionParams(
                url=MCP_SERVER_URL, timeout=30, sse_read_timeout=600
            )
        ),
        google_search,
    ],
    sub_agents=[stylish_agent],
    before_model_callback=extract_uploaded_images,
    before_tool_callback=inject_uploaded_images,
    after_tool_callback=handle_tool_response,
)

app = App(root_agent=style_advisor_agent, name="style_advisor_agent")
