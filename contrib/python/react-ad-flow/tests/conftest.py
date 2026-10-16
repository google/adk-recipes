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

"""Shared pytest fixtures for ad-campaign-agent tests.

This module provides:
- Database setup/teardown fixtures (using copies of main DB)
- Environment variable mocking
- GCS storage mocking
- Test data factories

IMPORTANT: Tests use a COPY of the main database (campaigns.db) to:
1. Preserve real demo data for testing
2. Avoid polluting the main database
3. Ensure tests are reproducible with consistent data
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add app to path for imports
APP_DIR = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(APP_DIR.parent))

# Path to the main database — must match app.config.DB_PATH (project root in
# local dev), otherwise _ensure_main_db_exists() populates one file and
# _copy_main_db_to_temp() copies a different, nonexistent one.
from app.config import DB_PATH as _CONFIG_DB_PATH  # noqa: E402

# Snapshot app.config's import-time state BEFORE any env fixture runs — see
# tests/_config_baseline.py (Phase 16 open item 6).
from tests._config_baseline import restore_config_baseline  # noqa: E402, F401

MAIN_DB_PATH = Path(_CONFIG_DB_PATH)


# =============================================================================
# Environment Fixtures
# =============================================================================


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up test environment variables before any tests run."""
    # Store original values
    original_env = {}
    test_env = {
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "GCS_BUCKET": "test-bucket",
        "GOOGLE_GENAI_USE_VERTEXAI": "True",
    }

    for key, value in test_env.items():
        original_env[key] = os.environ.get(key)
        os.environ[key] = value

    yield

    # Restore original values
    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# =============================================================================
# Database Fixtures
# =============================================================================


def _ensure_main_db_exists():
    """Ensure the main database exists with demo data.

    If the main database doesn't exist, initialize it with mock data.
    """
    if not MAIN_DB_PATH.exists():
        # Import and initialize if main DB doesn't exist
        from app.database.db import init_database
        from app.database.mock_data import seed_demo_data

        init_database()
        seed_demo_data()


def _copy_main_db_to_temp():
    """Copy the main database to a temporary file.

    Returns the path to the temp database.
    """
    _ensure_main_db_exists()

    # Create temp file for the copy
    fd, temp_path = tempfile.mkstemp(suffix=".db", prefix="test_campaigns_")
    os.close(fd)

    # Copy main database to temp location
    shutil.copy2(MAIN_DB_PATH, temp_path)

    return temp_path


@pytest.fixture(scope="function")
def test_db():
    """Create a COPY of the main database for each test function.

    This ensures:
    1. Tests use real demo data (campaigns, products, videos)
    2. Modifications don't affect the main database
    3. Each test starts with fresh, consistent data
    """
    db_path = _copy_main_db_to_temp()

    # Patch the DB_PATH in config to use our copy
    with patch("app.config.DB_PATH", db_path):
        yield db_path

    # Cleanup
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def shared_test_db():
    """Create a shared COPY of main database for a test module.

    More efficient for read-only tests that don't modify data.
    The same copy is shared across all tests in the module.
    """
    db_path = _copy_main_db_to_temp()

    with patch("app.config.DB_PATH", db_path):
        yield db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(scope="function")
def fresh_test_db():
    """Create a completely fresh database (not a copy) for isolation tests.

    Use this when you need a clean slate without existing demo data.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_fresh_")
    os.close(fd)

    with patch("app.config.DB_PATH", db_path):
        from app.database.db import init_database
        from app.database.mock_data import seed_demo_data

        init_database()
        seed_demo_data()

        yield db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(scope="function")
def empty_test_db():
    """Schema-only database — the DEMO_DATASET=none / from-scratch state.

    Phase 15: init_database() no longer seeds, so this is just init on a
    temp file. Use for onboarding-tool and empty-catalog tests.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_empty_")
    os.close(fd)

    with patch("app.config.DB_PATH", db_path):
        from app.database.db import init_database

        init_database()
        yield db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


# =============================================================================
# GCS Storage Fixtures
# =============================================================================


@pytest.fixture
def mock_gcs_storage():
    """Mock GCS storage operations for tests that don't need real cloud access."""
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    # Setup mock chain
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob
    mock_blob.public_url = (
        "https://storage.googleapis.com/test-bucket/test-file.mp4"
    )
    mock_blob.exists.return_value = True

    with patch("google.cloud.storage.Client", return_value=mock_client):
        yield {
            "client": mock_client,
            "bucket": mock_bucket,
            "blob": mock_blob,
        }


@pytest.fixture
def mock_storage_module():
    """Mock the entire storage module for complete isolation."""
    with patch.multiple(
        "app.storage",
        get_storage_mode=MagicMock(return_value="gcs"),
        list_seed_images=MagicMock(return_value=["image1.jpg", "image2.jpg"]),
        image_exists=MagicMock(return_value=True),
        video_exists=MagicMock(return_value=True),
        get_public_url=MagicMock(
            return_value="https://storage.googleapis.com/test-bucket/test.mp4"
        ),
        get_video_public_url=MagicMock(
            return_value="https://storage.googleapis.com/test-bucket/video.mp4"
        ),
        get_thumbnail_public_url=MagicMock(
            return_value="https://storage.googleapis.com/test-bucket/thumb.jpg"
        ),
        product_image_exists=MagicMock(return_value=True),
        get_product_image_public_url=MagicMock(
            return_value="https://storage.googleapis.com/test-bucket/product-images/test.png"
        ),
    ) as mocks:
        yield mocks


# =============================================================================
# Test Data Factories
# =============================================================================


@pytest.fixture
def sample_campaign():
    """Factory for creating sample campaign data."""
    return {
        "id": 1,
        "name": "Test Campaign - Store A",
        "product_id": 1,
        "store_name": "Test Store",
        "city": "San Francisco",
        "state": "California",
        "status": "active",
        "latitude": 37.7749,
        "longitude": -122.4194,
    }


@pytest.fixture
def sample_product():
    """Factory for creating sample product data."""
    return {
        "id": 1,
        "name": "Test Product",
        "category": "dress",
        "style": "elegant",
        "color": "black",
        "fabric": "silk",
        "details": "A beautiful test product",
        "occasion": "formal",
        "image_filename": "test-product.jpg",
    }


@pytest.fixture
def sample_video():
    """Factory for creating sample video data."""
    return {
        "id": 1,
        "campaign_id": 1,
        "product_id": 1,
        "video_filename": "test-video-122524-asian-beach.mp4",
        "thumbnail_filename": "test-video-122524-asian-beach-thumb.jpg",
        "status": "generated",
        "variation_name": "asian-beach",
        "variation_params": {
            "model_ethnicity": "asian",
            "setting": "beach",
            "mood": "romantic",
        },
    }


@pytest.fixture
def sample_variation():
    """Factory for creating sample variation parameters."""
    return {
        "model_ethnicity": "european",
        "setting": "urban",
        "mood": "sophisticated",
        "lighting": "natural",
        "time_of_day": "golden-hour",
        "activity": "walking",
        "camera_movement": "tracking",
    }


# =============================================================================
# Agent Fixtures
# =============================================================================


@pytest.fixture
def root_agent():
    """Get the root coordinator agent."""
    from app.agent import root_agent

    return root_agent


@pytest.fixture
def campaign_agent():
    """Get the campaign sub-agent."""
    from app.agent import campaign_agent

    return campaign_agent


@pytest.fixture
def media_agent():
    """Get the media sub-agent."""
    from app.agent import media_agent

    return media_agent


@pytest.fixture
def review_agent():
    """Get the review sub-agent."""
    from app.agent import review_agent

    return review_agent


@pytest.fixture
def analytics_agent():
    """Get the analytics sub-agent."""
    from app.agent import analytics_agent

    return analytics_agent


# =============================================================================
# Utility Fixtures
# =============================================================================


@pytest.fixture
def assert_tool_called():
    """Utility to check if a specific tool was called in events."""

    def _assert_tool_called(events, tool_name):
        tool_calls = []
        for event in events:
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        tool_calls.append(part.function_call.name)

        assert tool_name in tool_calls, (
            f"Expected tool '{tool_name}' to be called. "
            f"Actual calls: {tool_calls}"
        )

    return _assert_tool_called


@pytest.fixture
def extract_text_response():
    """Utility to extract text from agent response events."""

    def _extract_text(events):
        texts = []
        for event in events:
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        texts.append(part.text)
        return " ".join(texts)

    return _extract_text
