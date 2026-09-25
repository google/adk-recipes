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

import google.auth
from google.adk.tools.spanner import SpannerToolset
from google.adk.tools.spanner.settings import Capabilities, SpannerToolSettings
from google.adk.tools.spanner.spanner_credentials import (
    SpannerCredentialsConfig,
)

from ..config import config


class SpannerQueryTools:
    @classmethod
    def get_toolset(cls) -> list:
        """Provides a list containing the available SpannerToolset to be consumed by an agent."""
        if not config.is_spanner_configured:
            return []
        return [
            SpannerToolset(
                credentials_config=cls.get_credentials_config(),
                spanner_tool_settings=cls.get_tool_settings(),
            )
        ]

    @staticmethod
    def get_tool_settings():
        tool_settings = SpannerToolSettings(
            capabilities=[Capabilities.DATA_READ],
        )
        return tool_settings

    @staticmethod
    def get_credentials_config():
        application_default_credentials, _ = google.auth.default()
        credentials_config = SpannerCredentialsConfig(
            credentials=application_default_credentials,
        )
        return credentials_config
