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
from unittest.mock import patch

from presentation_agent.agent import PresentationExpertApp


@patch("presentation_agent.agent.initialize_genai_client")
def test_app_initialization(mock_init):
    # This ensures that all tools, memory, and artifact services can be composed properly
    # without running into syntax or initialization errors.
    os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

    app = PresentationExpertApp()

    # Assert main agent exists
    assert app._agent is not None
    assert app._agent.name == "presentation_expert_agent"

    # Assert it has tools
    assert len(app._agent.tools) > 0

    # Assert runner is composed
    assert app._runner is not None
    assert app._runner.app_name == "presentation_agent"


@patch("presentation_agent.agent.initialize_genai_client")
@patch("presentation_agent.agent.ENABLE_RAG", True)
def test_app_initialization_with_rag(mock_init):
    os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

    app = PresentationExpertApp()

    # Assert main agent exists
    assert app._agent is not None


@patch("presentation_agent.agent.initialize_genai_client")
@patch("presentation_agent.agent.GCS_BUCKET_NAME", None)
def test_app_initialization_no_gcs_bucket(mock_init):
    app = PresentationExpertApp()
    assert app._agent is not None


@patch("presentation_agent.agent.initialize_genai_client")
@patch("presentation_agent.agent.GCS_BUCKET_NAME", "my-bucket")
@patch("presentation_agent.agent.get_gcs_client")
def test_app_initialization_gcs_bucket_fail_none(mock_get_gcs, mock_init):
    mock_get_gcs.return_value = None
    app = PresentationExpertApp()
    assert app._agent is not None


@patch("presentation_agent.agent.initialize_genai_client")
@patch("presentation_agent.agent.GCS_BUCKET_NAME", "my-bucket")
@patch("presentation_agent.agent.get_gcs_client")
def test_app_initialization_gcs_bucket_exception(mock_get_gcs, mock_init):
    mock_get_gcs.side_effect = Exception("Failed")
    app = PresentationExpertApp()
    assert app._agent is not None
