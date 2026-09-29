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

"""Defines shared constants for brand search optimization."""

import os

AGENT_NAME = "brand_search_optimization"
DESCRIPTION = (
    "A multi-agent assistant for optimizing brand product search performance "
    "using Gemini Computer Use."
)
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "EMPTY")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
MODEL = os.getenv("MODEL")
DATASET_ID = os.getenv("DATASET_ID", "products_data_agent")
TABLE_ID = os.getenv("TABLE_ID", "shoe_items")
DISABLE_WEB_DRIVER = int(os.getenv("DISABLE_WEB_DRIVER", "0"))
STAGING_BUCKET = os.getenv("STAGING_BUCKET", "")
