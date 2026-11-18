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

"""Built-in stdio Google Workspace MCP servers spawned by the gateway with `GOOGLE_WORKSPACE_ACCESS_TOKEN`."""

SERVERS = {
    "gmail": "workspace_mcp.gmail",
    "google_calendar": "workspace_mcp.google_calendar",
    "google_drive": "workspace_mcp.drive",
    "google_docs": "workspace_mcp.docs",
    "google_sheets": "workspace_mcp.sheets",
    "google_slides": "workspace_mcp.slides",
}
