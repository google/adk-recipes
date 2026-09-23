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

from unittest.mock import MagicMock, patch

import presentation_agent.shared_libraries.config as config_module


def test_app_config_no_project_does_not_raise_exception():
    config_module._genai_client = None
    with patch("os.getenv", return_value=None):
        with patch(
            "presentation_agent.shared_libraries.config.genai.Client",
            side_effect=Exception("Test Exception"),
        ):
            # We are testing that initialize_genai_client does not raise an exception,
            # but returns None. The try/except block in the function handles the
            # exception.
            client = config_module.initialize_genai_client()
            assert client is None


def test_initialize_genai_client_success():
    config_module._genai_client = None
    with patch(
        "presentation_agent.shared_libraries.config.genai.Client"
    ) as mock_client:
        mock_client.return_value = MagicMock()
        client = config_module.initialize_genai_client()
        assert client is not None
        # Subsequent call should return the same client
        assert config_module.initialize_genai_client() == client


def test_initialize_genai_client_exception():
    config_module._genai_client = None
    with patch(
        "presentation_agent.shared_libraries.config.genai.Client"
    ) as mock_client:
        mock_client.side_effect = Exception("Test client init failure")
        client = config_module.initialize_genai_client()
        assert client is None


def test_get_gcs_client_success():
    with patch(
        "presentation_agent.shared_libraries.config.storage.Client"
    ) as mock_client:
        mock_client.return_value = MagicMock()
        client = config_module.get_gcs_client()
        assert client is not None


def test_get_gcs_client_exception():
    with patch(
        "presentation_agent.shared_libraries.config.storage.Client"
    ) as mock_client:
        mock_client.side_effect = Exception("Test GCS client init failure")
        client = config_module.get_gcs_client()
        assert client is None


def test_resolve_regional_location():
    assert config_module.resolve_regional_location(None) == "us-central1"
    assert config_module.resolve_regional_location("") == "us-central1"
    assert config_module.resolve_regional_location("global") == "us-central1"
    assert config_module.resolve_regional_location("GLOBAL") == "us-central1"
    assert config_module.resolve_regional_location("us") == "us-central1"
    assert config_module.resolve_regional_location("US") == "us-central1"
    assert config_module.resolve_regional_location("us-east1") == "us-east1"
    assert (
        config_module.resolve_regional_location("europe-west1")
        == "europe-west1"
    )
