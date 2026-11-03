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

"""One-sentence descriptions of user images.

Used by the Studio for uploads (from the panel or attached to a chat
message) and preset model picks: the agent gets the description and a
``gs://`` reference, not the pixels.
"""

from genmedia4commerce.core import gen
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models

__all__ = ["DESCRIBE_PROMPT", "describe_image"]

DESCRIBE_PROMPT = (
    "Describe this image in one detailed sentence. "
    "If it's a person, mention gender, pose, visible clothing, and setting. "
    "If it's a garment or product, mention the type (shirt, dress, shoes, sunglasses...), "
    "color, pattern, brand/logo if visible, material, and any distinctive details "
    "(e.g. 'white AURELIA sneakers with a red wave logo and chunky sole'). "
    "Be specific enough that someone could distinguish this item from similar ones."
)


def describe_image(image: bytes) -> str:
    """One detailed sentence about ``image`` (``text_lite``, no thinking)."""
    params = TextParams(model=get_models().text_lite, thinking="OFF")
    return gen.generate_text([DESCRIBE_PROMPT, image], params=params).strip()
