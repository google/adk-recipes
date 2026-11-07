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

"""Tools for the Ad Campaign Agent."""

from .campaign_tools import (
    create_campaign,
    get_campaign,
    list_campaigns,
    update_campaign,
)
from .image_tools import (
    add_seed_image,
    analyze_image,
    list_available_images,
    # generate_seed_image removed - retailers provide product images
    list_campaign_images,
)
from .maps_tools import (
    get_campaign_locations,
    get_location_demographics,
    search_nearby_stores,
)
from .metrics_tools import (
    compare_campaigns,
    get_campaign_insights,
    get_campaign_metrics,
    get_top_performing_ads,
)
from .video_tools import (
    generate_video_ad,
    generate_video_prompt,
    list_campaign_ads,
)

__all__ = [
    # Image management
    "add_seed_image",
    "analyze_image",
    "compare_campaigns",
    # Campaign management
    "create_campaign",
    # "generate_seed_image" removed
    # Video generation
    "generate_video_ad",
    "generate_video_prompt",
    "get_campaign",
    "get_campaign_insights",
    # Maps
    "get_campaign_locations",
    # Metrics
    "get_campaign_metrics",
    "get_location_demographics",
    "get_top_performing_ads",
    "list_available_images",
    "list_campaign_ads",
    "list_campaign_images",
    "list_campaigns",
    "search_nearby_stores",
    "update_campaign",
]
