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

"""Lock the App-level context cache wiring.

The 3-tier system prompt has a large stable head whose bytes do not
change across turns within a session. Without ``ContextCacheConfig`` on
``App(...)``, every turn re-ingests those bytes — wasted Vertex spend
and added latency. This test pins the config so future refactors don't
silently drop it.
"""

from __future__ import annotations

from horizon.agent import app


def test_app_has_context_cache_config():
    assert app.context_cache_config is not None


def test_app_context_cache_min_tokens():
    # 4096, not 2048: Gemini's own per-model floor
    # (gemini_context_cache_manager.py's _minimum_cache_tokens) is hardcoded
    # to 4096 for any gemini-3* model, so 2048 was dead config that never
    # bound for horizon's default model.
    assert app.context_cache_config.min_tokens == 4096


def test_app_context_cache_ttl_seconds():
    assert app.context_cache_config.ttl_seconds == 1800


def test_app_context_cache_intervals():
    assert app.context_cache_config.cache_intervals == 10
