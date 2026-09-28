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

"""Unit tests for the ADK agent definition."""

import base64


class TestAgentDefinition:
    """Tests for the agent module."""

    def test_agent_imports(self):
        """Agent module should import without errors."""
        from app.agent import app, root_agent

        assert root_agent is not None
        assert app is not None

    def test_agent_name(self):
        """Agent should have the correct name."""
        from app.agent import root_agent

        assert root_agent.name == "genmedia_router"

    def test_agent_has_tools(self):
        """Agent should have the product_fitting tool."""
        from app.agent import root_agent

        assert root_agent.tools is not None
        assert len(root_agent.tools) > 0

    def test_agent_has_after_tool_callback(self):
        """Agent should have the after_tool_callback for image extraction."""
        from app.agent import root_agent

        assert root_agent.after_tool_callback is not None

    def test_app_name(self):
        """App should have the correct name."""
        from app.agent import app

        assert app.name == "app"


class TestStripImagesFromResult:
    """Tests for the _strip_images_from_result helper."""

    def test_no_images(self):
        """Should return the same dict when no base64 images are present."""
        from app.agents.style_advisor_agent.agent import (
            _strip_images_from_result,
        )

        result = {"status": "ok", "message": "done"}
        clean, images = _strip_images_from_result(result, "test_tool")

        assert clean == result
        assert images == {}

    def test_top_level_base64_extraction(self):
        """Should extract top-level *_base64 keys with large values."""
        from app.agents.style_advisor_agent.agent import (
            _strip_images_from_result,
        )

        fake_image = b"\x89PNG" + b"\x00" * 2000
        b64_str = base64.b64encode(fake_image).decode()

        result = {"image_base64": b64_str, "quality_score": 0.95}
        clean, images = _strip_images_from_result(result, "product_fitting")

        assert "image_base64" not in clean
        assert "image_artifact" in clean
        assert clean["quality_score"] == 0.95
        assert len(images) == 1
        assert "product_fitting_image.png" in images

    def test_nested_base64_extraction(self):
        """Should extract base64 from nested dicts (e.g. front/back views)."""
        from app.agents.style_advisor_agent.agent import (
            _strip_images_from_result,
        )

        fake_image = b"\x89PNG" + b"\x00" * 2000
        b64_str = base64.b64encode(fake_image).decode()

        result = {
            "front": {"image_base64": b64_str, "score": 0.9},
            "back": {"image_base64": b64_str, "score": 0.8},
        }
        clean, images = _strip_images_from_result(result, "product_fitting")

        assert clean["front"]["score"] == 0.9
        assert "image_base64" not in clean["front"]
        assert "image_artifact" in clean["front"]
        assert len(images) == 2

    def test_short_base64_not_extracted(self):
        """Should not extract short base64 strings (< 1000 chars)."""
        from app.agents.style_advisor_agent.agent import (
            _strip_images_from_result,
        )

        result = {"thumbnail_base64": "abc123"}
        clean, images = _strip_images_from_result(result, "test_tool")

        assert clean == result
        assert images == {}

    def test_none_values_preserved(self):
        """Should preserve None values."""
        from app.agents.style_advisor_agent.agent import (
            _strip_images_from_result,
        )

        result = {"front": None, "status": "ok"}
        clean, images = _strip_images_from_result(result, "test_tool")

        assert clean["front"] is None
        assert images == {}
