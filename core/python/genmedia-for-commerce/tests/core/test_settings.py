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

from pathlib import Path

import pytest

from genmedia4commerce.core.errors import ConfigError
from genmedia4commerce.core.settings import get_settings

_ENV_VARS = (
    "PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GLOBAL_REGION",
    "DEFAULT_REGION",
    "GOOGLE_API_KEY",
    "MEDIA_BUCKET",
    "GENMEDIA_DEBUG_DIR",
    "GENMEDIA_CATALOG_PATH",
    "GENMEDIA_STYLE_GUIDELINES",
    "GENMEDIA_SCENARIO",
    "GENMEDIA_ALLOWED_IMAGE_HOSTS",
    "GENMEDIA_RUN_MODE",
    "GENMEDIA_NUM_VARIATIONS",
    "GENMEDIA_MAX_ATTEMPTS",
    "GENMEDIA_VIDEOS_PER_IMAGE",
    "GENMEDIA_TRYON_CLOSEUPS",
    "GENMEDIA_USE_CACHE",
    "GENMEDIA_UPSCALE_FACE",
    "GENMEDIA_TRYON_FIX",
    "GENMEDIA_OUTPUT",
    "GENMEDIA_INPUT",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    return monkeypatch


def test_defaults(clean_env):
    clean_env.setenv("PROJECT_ID", "p1")
    s = get_settings()
    assert s.project_id == "p1"
    assert s.location == "global"
    assert s.region == "us-central1"
    assert s.google_api_key is None
    assert s.media_bucket == "p1-genmedia-for-commerce-media-payloads"
    assert s.catalog_path == (
        Path(__file__).parents[2] / "assets" / "catalog" / "products.parquet"
    )
    assert s.debug_dir is None
    assert s.style_guidelines == ""
    assert s.scenario == "a plain light grey studio environment"
    assert s.allowed_image_hosts == (
        "storage.googleapis.com",
        "storage.cloud.google.com",
    )
    assert s.run_mode == "parallel"
    assert s.num_variations == 3
    assert s.max_attempts == 5
    assert s.videos_per_image == 2


def test_env_override(clean_env, tmp_path):
    env = {
        "PROJECT_ID": "p2",
        "GLOBAL_REGION": "europe-west1",
        "DEFAULT_REGION": "europe-west4",
        "GOOGLE_API_KEY": "key",
        "MEDIA_BUCKET": "my-bucket",
        "GENMEDIA_CATALOG_PATH": str(tmp_path / "c.parquet"),
        "GENMEDIA_DEBUG_DIR": str(tmp_path / "d"),
        "GENMEDIA_STYLE_GUIDELINES": "jackets closed",
        "GENMEDIA_SCENARIO": "a beach",
        "GENMEDIA_RUN_MODE": "sequential",
        "GENMEDIA_NUM_VARIATIONS": "2",
        "GENMEDIA_MAX_ATTEMPTS": "7",
        "GENMEDIA_VIDEOS_PER_IMAGE": "3",
    }
    for k, v in env.items():
        clean_env.setenv(k, v)
    s = get_settings()
    assert s.project_id == "p2"
    assert s.location == "europe-west1"
    assert s.region == "europe-west4"
    assert s.google_api_key == "key"
    assert s.media_bucket == "my-bucket"
    assert s.catalog_path == tmp_path / "c.parquet"
    assert s.debug_dir == tmp_path / "d"
    assert s.style_guidelines == "jackets closed"
    assert s.scenario == "a beach"
    assert s.run_mode == "sequential"
    assert s.num_variations == 2
    assert s.max_attempts == 7
    assert s.videos_per_image == 3


def test_google_cloud_project_fallback(clean_env):
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
    assert get_settings().project_id == "gcp-proj"


def test_project_id_wins_over_fallback(clean_env):
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
    clean_env.setenv("PROJECT_ID", "pid")
    assert get_settings().project_id == "pid"


def test_missing_project_raises_only_on_access(clean_env):
    s = get_settings()  # must not raise
    assert s.location == "global"
    with pytest.raises(ConfigError):
        _ = s.project_id


def test_media_bucket_needs_project_when_not_set(clean_env):
    s = get_settings()
    with pytest.raises(ConfigError):
        _ = s.media_bucket


def test_allowed_image_hosts_parsing(clean_env):
    clean_env.setenv("GENMEDIA_ALLOWED_IMAGE_HOSTS", " a.com, b.org ,,c.net ")
    assert get_settings().allowed_image_hosts == ("a.com", "b.org", "c.net")


def test_invalid_int_raises_config_error(clean_env):
    clean_env.setenv("GENMEDIA_NUM_VARIATIONS", "three")
    with pytest.raises(ConfigError):
        get_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GENMEDIA_NUM_VARIATIONS", "0"),
        ("GENMEDIA_NUM_VARIATIONS", "-1"),
        ("GENMEDIA_NUM_VARIATIONS", "6"),
        ("GENMEDIA_MAX_ATTEMPTS", "0"),
        ("GENMEDIA_MAX_ATTEMPTS", "11"),
        ("GENMEDIA_MAX_ATTEMPTS", "1000"),
        ("GENMEDIA_VIDEOS_PER_IMAGE", "0"),
        ("GENMEDIA_VIDEOS_PER_IMAGE", "6"),
    ],
)
def test_out_of_range_int_raises_config_error(clean_env, name, value):
    clean_env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        get_settings()


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("GENMEDIA_NUM_VARIATIONS", "1", "num_variations"),
        ("GENMEDIA_NUM_VARIATIONS", "5", "num_variations"),
        ("GENMEDIA_MAX_ATTEMPTS", "1", "max_attempts"),
        ("GENMEDIA_MAX_ATTEMPTS", "10", "max_attempts"),
        ("GENMEDIA_VIDEOS_PER_IMAGE", "1", "videos_per_image"),
        ("GENMEDIA_VIDEOS_PER_IMAGE", "5", "videos_per_image"),
    ],
)
def test_int_range_bounds_are_accepted(clean_env, name, value, field):
    clean_env.setenv(name, value)
    assert getattr(get_settings(), field) == int(value)


def test_invalid_run_mode_raises_config_error(clean_env):
    clean_env.setenv("GENMEDIA_RUN_MODE", "random")
    with pytest.raises(ConfigError):
        get_settings()


def test_cached_until_cache_clear(clean_env):
    clean_env.setenv("PROJECT_ID", "first")
    s1 = get_settings()
    clean_env.setenv("PROJECT_ID", "second")
    assert get_settings() is s1
    assert get_settings().project_id == "first"
    get_settings.cache_clear()
    assert get_settings().project_id == "second"


def test_settings_frozen(clean_env):
    s = get_settings()
    with pytest.raises(AttributeError):
        s.location = "x"


def test_bool_setting_default_true(clean_env):
    assert get_settings().tryon_closeups is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("0", False),
        ("off", False),
        ("TRUE", True),
        ("1", True),
    ],
)
def test_bool_setting_env(clean_env, raw, expected):
    clean_env.setenv("GENMEDIA_TRYON_CLOSEUPS", raw)
    assert get_settings().tryon_closeups is expected


def test_the_preprocessing_cache_is_on_unless_turned_off(clean_env):
    assert get_settings().use_cache is True
    clean_env.setenv("GENMEDIA_USE_CACHE", "false")
    get_settings.cache_clear()
    assert get_settings().use_cache is False


def test_face_upscaling_is_off_unless_turned_on(clean_env):
    assert get_settings().upscale_face is False
    clean_env.setenv("GENMEDIA_UPSCALE_FACE", "true")
    get_settings.cache_clear()
    assert get_settings().upscale_face is True


def test_the_try_on_fix_pass_is_off_unless_turned_on(clean_env):
    assert get_settings().tryon_fix is False
    clean_env.setenv("GENMEDIA_TRYON_FIX", "true")
    get_settings.cache_clear()
    assert get_settings().tryon_fix is True


def test_bool_setting_invalid(clean_env):
    clean_env.setenv("GENMEDIA_TRYON_CLOSEUPS", "maybe")
    with pytest.raises(ConfigError):
        get_settings()


@pytest.mark.parametrize("field", ["input_mode", "output_mode"])
def test_storage_modes_default_inline(clean_env, field):
    assert getattr(get_settings(), field) == "inline"


@pytest.mark.parametrize(
    ("var", "field"),
    [("GENMEDIA_OUTPUT", "output_mode"), ("GENMEDIA_INPUT", "input_mode")],
)
@pytest.mark.parametrize("raw", ["inline", "gcs"])
def test_storage_mode_env(clean_env, var, field, raw):
    clean_env.setenv(var, raw)
    assert getattr(get_settings(), field) == raw


def test_input_and_output_modes_are_independent(clean_env):
    clean_env.setenv("GENMEDIA_INPUT", "gcs")
    s = get_settings()
    assert (s.input_mode, s.output_mode) == ("gcs", "inline")


@pytest.mark.parametrize("var", ["GENMEDIA_OUTPUT", "GENMEDIA_INPUT"])
@pytest.mark.parametrize("raw", ["s3", "GCS", "base64"])
def test_storage_mode_invalid(clean_env, var, raw):
    clean_env.setenv(var, raw)
    with pytest.raises(ConfigError, match=var):
        get_settings()
