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

import pytest

from genmedia4commerce.core.models import get_models

# field -> (env var, default) exactly as spec §2
_REGISTRY = {
    "text": ("GENMEDIA_TEXT_MODEL", "gemini-3.8-flash"),
    "text_lite": ("GENMEDIA_TEXT_LITE_MODEL", "gemini-3.5-flash-lite"),
    "judge": ("GENMEDIA_JUDGE_MODEL", "gemini-4-argon"),
    "image": ("GENMEDIA_IMAGE_MODEL", "gemini-nano-banana-2.1"),
    "video": ("GENMEDIA_VIDEO_MODEL", "gemini-omni-1.1-flash-preview"),
    "video_api_revision": ("GENMEDIA_VIDEO_API_REVISION", "2026-05-20"),
    "embed_catalog": ("GENMEDIA_EMBED_CATALOG_MODEL", "gemini-embedding-001"),
}


@pytest.fixture
def clean_env(monkeypatch):
    for env, _ in _REGISTRY.values():
        monkeypatch.delenv(env, raising=False)
    get_models.cache_clear()
    return monkeypatch


@pytest.mark.parametrize("field", list(_REGISTRY))
def test_default(clean_env, field):
    assert getattr(get_models(), field) == _REGISTRY[field][1]


@pytest.mark.parametrize("field", list(_REGISTRY))
def test_env_override(clean_env, field):
    env, _ = _REGISTRY[field]
    clean_env.setenv(env, f"custom-{field}")
    assert getattr(get_models(), field) == f"custom-{field}"


def test_cached(clean_env):
    first = get_models()
    clean_env.setenv("GENMEDIA_TEXT_MODEL", "other")
    assert get_models() is first
    get_models.cache_clear()
    assert get_models().text == "other"
