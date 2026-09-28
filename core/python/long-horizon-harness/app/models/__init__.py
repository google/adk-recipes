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

"""Root-agent model registry + per-session dispatch.

See ``app/models/registry.py`` for the available backends.
"""

from __future__ import annotations

from app.models.dispatcher import DispatchingLlm
from app.models.registry import DEFAULT_MODEL_NAME, MODEL_REGISTRY
from app.models.selector import (
    SELECTED_MODEL_STATE_KEY,
    select_model_callback,
)


def build_root_llm() -> DispatchingLlm:
    return DispatchingLlm(model=DEFAULT_MODEL_NAME, backends=MODEL_REGISTRY)


__all__ = [
    "DEFAULT_MODEL_NAME",
    "MODEL_REGISTRY",
    "SELECTED_MODEL_STATE_KEY",
    "DispatchingLlm",
    "build_root_llm",
    "select_model_callback",
]
