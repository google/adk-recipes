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

"""Deployment settings read once from the environment.

No network, no SDK import and no file download happen here. ``project_id``
(and the ``media_bucket`` derived from it) are validated on first access, so
the package imports without credentials.
"""

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from genmedia4commerce.core.errors import ConfigError

__all__ = ["Mode", "Settings", "export_genai_env", "get_settings"]

_REPO_ROOT = Path(__file__).parents[2]
Mode = Literal["inline", "gcs"]  # where inputs / results are kept
RunMode = Literal["parallel", "sequential"]


@dataclass(frozen=True)
class Settings:
    """Deployment settings. Build with ``get_settings()``."""

    _project_id: str = ""
    location: str = "global"
    region: str = "us-central1"
    google_api_key: str | None = None
    _media_bucket: str = ""
    catalog_path: Path = _REPO_ROOT / "assets" / "catalog" / "products.parquet"
    style_guidelines: str = ""
    scenario: str = "a plain light grey studio environment"
    allowed_image_hosts: tuple[str, ...] = (
        "storage.googleapis.com",
        "storage.cloud.google.com",
    )
    run_mode: RunMode = "parallel"
    num_variations: int = 3
    max_attempts: int = 5
    videos_per_image: int = 2  # video try-on: videos of each animated image
    spin_closeups: bool = True  # close-up references of small details
    tryon_closeups: bool = True  # detail close-ups in the try-on prompt
    use_cache: bool = True  # photo try-on preprocessing cache (core.cache)
    upscale_face: bool = False  # upscale the try-on prompt's face crop
    tryon_fix: bool = False  # one fix pass for a judge-rejected try-on image
    input_mode: Mode = "inline"
    output_mode: Mode = "inline"
    debug_dir: Path | None = None  # local folder for run traces (core.trace)

    @property
    def project_id(self) -> str:
        """Return the GCP project id; ``ConfigError`` when it is not set."""
        if not self._project_id:
            raise ConfigError(
                "PROJECT_ID is not set (nor GOOGLE_CLOUD_PROJECT). "
                "Set it in config.env or the environment."
            )
        return self._project_id

    @property
    def media_bucket(self) -> str:
        """Return the bucket for user uploads and generated media."""
        return (
            self._media_bucket
            or f"{self.project_id}-genmedia-for-commerce-media-payloads"
        )


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be {low} to {high}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _env(name, default)
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {allowed}")
    return value


def _env_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser() if raw else None


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings from the environment once (cached).

    Tests call ``get_settings.cache_clear()`` after changing the environment.
    """
    hosts = _env("GENMEDIA_ALLOWED_IMAGE_HOSTS")
    return Settings(
        _project_id=_env("PROJECT_ID") or _env("GOOGLE_CLOUD_PROJECT"),
        location=_env("GLOBAL_REGION", "global"),
        region=_env("DEFAULT_REGION", "us-central1"),
        google_api_key=_env("GOOGLE_API_KEY") or None,
        _media_bucket=_env("MEDIA_BUCKET"),
        catalog_path=_env_path("GENMEDIA_CATALOG_PATH")
        or Settings.catalog_path,
        style_guidelines=_env("GENMEDIA_STYLE_GUIDELINES"),
        scenario=_env("GENMEDIA_SCENARIO", Settings.scenario),
        allowed_image_hosts=(
            tuple(h.strip() for h in hosts.split(",") if h.strip())
            if hosts
            else Settings.allowed_image_hosts
        ),
        run_mode=cast(
            RunMode,
            _env_choice("GENMEDIA_RUN_MODE", "parallel", get_args(RunMode)),
        ),
        num_variations=_env_int("GENMEDIA_NUM_VARIATIONS", 3, 1, 5),
        max_attempts=_env_int("GENMEDIA_MAX_ATTEMPTS", 5, 1, 10),
        videos_per_image=_env_int("GENMEDIA_VIDEOS_PER_IMAGE", 2, 1, 5),
        spin_closeups=_env_bool("GENMEDIA_SPIN_CLOSEUPS", True),
        tryon_closeups=_env_bool("GENMEDIA_TRYON_CLOSEUPS", True),
        use_cache=_env_bool("GENMEDIA_USE_CACHE", True),
        upscale_face=_env_bool("GENMEDIA_UPSCALE_FACE", False),
        tryon_fix=_env_bool("GENMEDIA_TRYON_FIX", False),
        input_mode=cast(
            Mode, _env_choice("GENMEDIA_INPUT", "inline", get_args(Mode))
        ),
        output_mode=cast(
            Mode, _env_choice("GENMEDIA_OUTPUT", "inline", get_args(Mode))
        ),
        debug_dir=_env_path("GENMEDIA_DEBUG_DIR"),
    )


def export_genai_env() -> None:
    """Set the variables the ADK model reads to build its own genai client."""
    s = get_settings()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", os.getenv("PROJECT_ID", ""))
    os.environ["GOOGLE_CLOUD_LOCATION"] = s.location
    use_vertex = "False" if s.google_api_key else "True"
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", use_vertex)
