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

"""Serve the built React frontend as a single-page app (SPA)."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse


def attach_spa_routes(app: FastAPI, frontend_dir: Path) -> None:
    """Register the SPA catch-all route for the frontend build in frontend_dir.

    Register it after every other route: it matches any GET path.
    """
    frontend_root = frontend_dir.resolve()
    index_html = frontend_root / "index.html"

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Resolve the requested path and only serve it if it is still inside
        # dist/. Joining an absolute full_path ("GET //etc/passwd") discards
        # frontend_root, and ".." segments ("GET /..%2F..%2Fetc%2Fpasswd")
        # walk out of it.
        try:
            file_path = (frontend_root / full_path).resolve()
        except ValueError:  # e.g. an embedded null byte ("GET /%00")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
        if not file_path.is_relative_to(frontend_root):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        # If the path matches an actual file in dist/, serve it
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        # Only serve index.html for SPA routes (paths without file extensions)
        # Asset requests (.json, .js, .css, etc.) that don't exist should 404
        if "." in full_path.rsplit("/", maxsplit=1)[-1]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(index_html)
