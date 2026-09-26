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

import os

from dotenv import load_dotenv
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

load_dotenv()

MAPS_MCP_URL = "https://mapstools.googleapis.com/mcp"


def get_places_toolset() -> McpToolset:
    """Return an MCP toolset connected to Google Maps Grounding Lite.

    The toolset includes a tool called `search_places` that can be used to
    search for places and retrieve their details,
    including latitude, longitude, place_id, and a map link.

    The tool accepts a place name or address as input and returns the
    corresponding place details. This can be used to verify the information
    of places suggested by other agents or tools in the travel concierge application.
    """
    maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not maps_api_key:
        raise OSError("GOOGLE_MAPS_API_KEY must be set.")

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=MAPS_MCP_URL,
            headers={
                "X-Goog-Api-Key": maps_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        ),
        errlog=None,
    )


# The toolset is created on demand to avoid import-time dependency on environment configuration.
