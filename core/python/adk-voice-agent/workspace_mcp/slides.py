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

"""Google Slides tools (REST: slides.googleapis.com/v1) for the `google_slides` server in `.mcp.json`: `python -m workspace_mcp.slides`."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from workspace_mcp._google import GoogleApi
from workspace_mcp._server import Server

BASE = "https://slides.googleapis.com/v1"
TEXT_LIMIT = 20000

server = Server("google_slides")


def _text_content(text: dict[str, Any] | None) -> str:
    return "".join(
        ((element.get("textRun") or {}).get("content") or "")
        for element in (text or {}).get("textElements") or ()
    )


def _element_texts(elements: list[dict[str, Any]] | None) -> list[str]:
    parts: list[str] = []
    for element in elements or ():
        if "shape" in element:
            parts.append(_text_content(element["shape"].get("text")))
        elif "table" in element:
            for row in element["table"].get("tableRows") or ():
                for cell in row.get("tableCells") or ():
                    parts.append(_text_content(cell.get("text")))
        elif "elementGroup" in element:
            parts.extend(
                _element_texts(element["elementGroup"].get("children"))
            )
    return [p.strip() for p in parts if p.strip()]


def _presentation_id(args: dict[str, Any]) -> str:
    value = str(args["presentation_id"]).strip()
    if not value or "/" in value:
        raise ValueError(
            "`presentation_id` should be the ID from the Slides URL, not a URL or path"
        )
    return value


@server.tool(
    "get_presentation",
    "Read a Google Slides presentation: its title and each slide's ID, "
    "number, and text.",
    {
        "type": "object",
        "properties": {
            "presentation_id": {
                "type": "string",
                "description": "The presentation ID (from its URL or a Drive search).",
            }
        },
        "required": ["presentation_id"],
    },
    read_only=True,
)
async def get_presentation(api: GoogleApi, args: dict[str, Any]) -> dict:
    presentation_id = _presentation_id(args)
    data = await api.get(f"{BASE}/presentations/{quote(presentation_id)}")
    slides: list[dict[str, Any]] = []
    remaining, truncated = TEXT_LIMIT, False
    for number, slide in enumerate(data.get("slides") or (), start=1):
        text = "\n".join(_element_texts(slide.get("pageElements")))
        if len(text) > remaining:
            text, truncated = text[:remaining], True
        remaining -= len(text)
        slides.append(
            {
                "slide_id": slide.get("objectId", ""),
                "number": number,
                "text": text,
            }
        )
    result: dict[str, Any] = {
        "presentation_id": data.get("presentationId", presentation_id),
        "title": data.get("title", ""),
        "slides": slides,
    }
    if truncated:
        result["truncated"] = True
    return result


if __name__ == "__main__":
    server.run()
