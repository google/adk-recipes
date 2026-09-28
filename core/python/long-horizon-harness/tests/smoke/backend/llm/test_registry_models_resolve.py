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

"""Every registry model id must actually resolve at Vertex.

``gemini-3.1-pro`` sat in the registry from the Gemini-only migration until it
was found by hand: the real id is ``gemini-3.1-pro-preview``, and every unit
test that named the model asserted on strings or token limits without ever
dispatching, so nothing failed. Selecting it via /model or delegate returned
404 at runtime.
"""

from __future__ import annotations

import os

import pytest

from app.models.registry import MODEL_REGISTRY

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RUN_SMOKE") and os.environ.get("RUN_SMOKE_LLM")),
    reason="needs RUN_SMOKE=1 RUN_SMOKE_LLM=1 (bills real Vertex calls)",
)


@pytest.mark.parametrize("model_name", sorted(MODEL_REGISTRY))
def test_registry_model_answers(model_name: str) -> None:
    from google import genai

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )
    response = client.models.generate_content(
        model=model_name, contents="Reply with the single word: ok"
    )
    assert (response.text or "").strip(), f"{model_name} returned no text"
