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

"""Request and result types of the 360 spin workflow."""

from dataclasses import dataclass
from typing import Literal

from genmedia4commerce.core.gen import VideoParams
from genmedia4commerce.core.runner import RunPolicy

__all__ = ["ProductType", "SpinClassification", "SpinRequest"]

ProductType = Literal["shoes", "other"]


@dataclass
class SpinRequest:
    """Inputs of ``run_spin``.

    ``policy=None`` -> ``RunPolicy.from_settings()``; ``video=None`` ->
    ``VideoParams(aspect_ratio="16:9")``.
    """

    product_images: list[bytes]
    policy: RunPolicy | None = None
    video: VideoParams | None = None


@dataclass
class SpinClassification:
    """Product type, per-image view/rank and a one-line description.

    ``views[i] = {"index": i, "view": "front|back|left|right|other",
    "rank": 1 = the best image of its view, "presentation": "flat|worn"}``,
    one entry per input image, in input order.
    """

    product_type: ProductType
    views: list[dict]
    description: str
