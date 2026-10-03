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

"""Regression tests for SPA static file serving absolute path traversal security."""

import tempfile
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse


@pytest.mark.asyncio
async def test_serve_spa_blocks_absolute_path_traversal(monkeypatch):
    """Ensure absolute paths (e.g. //etc/passwd) cannot bypass frontend_dir prefix."""
    from genmedia4commerce import fast_api_app

    with tempfile.TemporaryDirectory() as tmpdir:
        dist_dir = Path(tmpdir)
        index_file = dist_dir / "index.html"
        index_file.write_text("<html>index</html>")
        safe_asset = dist_dir / "app.js"
        safe_asset.write_text("console.log('safe app');")

        class MockRoot:

            def __truediv__(self, other):
                if other == "frontend":

                    class MockFrontend:

                        def __truediv__(self, other2):
                            if other2 == "dist":
                                return dist_dir

                    return MockFrontend()
                return fast_api_app.PROJECT_ROOT / other

        monkeypatch.setattr(fast_api_app, "PROJECT_ROOT", MockRoot())
        fast_api_app._mount_frontend()

        serve_spa = None
        for route in fast_api_app.app.routes:
            if getattr(route, "name", "") == "serve_spa":
                serve_spa = route.endpoint
                break
        assert serve_spa is not None

        # Normal relative path within dist works
        resp = await serve_spa("app.js")
        assert isinstance(resp, FileResponse)
        assert Path(resp.path).resolve() == safe_asset.resolve()

        # Absolute path payload should be rejected with 404, not serve /etc/passwd
        resp_abs = await serve_spa("/etc/passwd")
        assert not (
            isinstance(resp_abs, FileResponse)
            and Path(resp_abs.path).resolve() == Path("/etc/passwd").resolve()
        )
        assert isinstance(resp_abs, JSONResponse)
        assert resp_abs.status_code == 404
