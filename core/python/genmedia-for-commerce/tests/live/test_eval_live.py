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

"""Live eval checks against real models (run with GENMEDIA_LIVE=1)."""

import logging

import pytest

from genmedia4commerce.core import catalog
from genmedia4commerce.eval.garment import evaluate_product_consistency

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.live

SHOES = "s_aura_running_shoe"  # a spin set of the catalogue


def test_product_consistency_between_two_views_of_a_shoe():
    reference, target = catalog.image(SHOES, 0), catalog.image(SHOES, 1)
    verdict = evaluate_product_consistency(
        [reference], target, product_filter="footwear"
    )
    logger.info(f"verdict: {verdict}")
    assert not verdict.error, verdict.description
    assert verdict.description
    assert len(verdict.details["per_reference"]) == 1
