# Copyright 2025 Google LLC
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

"""Configuration for the Ad Campaign Agent."""

import os
from enum import StrEnum


# App mode (Phase 6): demo|connected, default demo. Nothing consumes this yet —
# Phase 11a resolves it internally to a data-provider selection (demo → the
# synthetic provider, connected → the live one). APP_MODE stays the ONLY
# user-facing mode knob; do not add a second mode env var. (DEMO_DATASET
# below is NOT a mode — it's a demo-scoped dataset selector: which demo
# catalog gets seeded at startup.)
class AppMode(StrEnum):
    DEMO = "demo"
    CONNECTED = "connected"


_raw_app_mode = (os.environ.get("APP_MODE") or "").strip().lower()
try:
    APP_MODE = AppMode(_raw_app_mode) if _raw_app_mode else AppMode.DEMO
except ValueError:
    raise ValueError(
        f"Invalid APP_MODE={_raw_app_mode!r}. Allowed values: "
        f"{', '.join(m.value for m in AppMode)}; unset defaults to 'demo'."
    ) from None


# Demo dataset selector (Phase 15): which demo catalog to seed at startup.
# fashion (default) = today's full demo: 22 fashion products, the retail core
# test set, and 4 demo campaigns with metrics. none = schema only — an empty
# catalog for from-scratch product onboarding.
class DemoDataset(StrEnum):
    FASHION = "fashion"
    NONE = "none"


_raw_demo_dataset = (os.environ.get("DEMO_DATASET") or "").strip().lower()
try:
    DEMO_DATASET = (
        DemoDataset(_raw_demo_dataset)
        if _raw_demo_dataset
        else DemoDataset.FASHION
    )
except ValueError:
    raise ValueError(
        f"Invalid DEMO_DATASET={_raw_demo_dataset!r}. Allowed values: "
        f"{', '.join(d.value for d in DemoDataset)}; unset defaults to 'fashion'."
    ) from None


# BlueZoo `valid` policy (Phase 11b). Rule R — count only rows BlueZoo marked
# `valid` (a one-way commissioning-acceptance flag; see docs/METRICS.md) — is
# OUR recommendation, NOT yet confirmed by BlueZoo. It therefore ships as the
# DEFAULT of this explicit, configurable knob, never as a silent constant:
# flipping it requires zero code, and the live conformer logs the active
# policy + row count on every read. include-all = no filter, for
# reconciliation/debugging or the day BlueZoo answers differently. Consumed
# only by app/audience/live_bluezoo.py; demo mode ignores it.
class BlueZooValidPolicy(StrEnum):
    VALID_ONLY = "valid-only"
    INCLUDE_ALL = "include-all"


_raw_bluezoo_valid_policy = (
    (os.environ.get("BLUEZOO_VALID_POLICY") or "").strip().lower()
)
try:
    BLUEZOO_VALID_POLICY = (
        BlueZooValidPolicy(_raw_bluezoo_valid_policy)
        if _raw_bluezoo_valid_policy
        else BlueZooValidPolicy.VALID_ONLY
    )
except ValueError:
    raise ValueError(
        f"Invalid BLUEZOO_VALID_POLICY={_raw_bluezoo_valid_policy!r}. Allowed values: "
        f"{', '.join(p.value for p in BlueZooValidPolicy)}; unset defaults to 'valid-only'."
    ) from None

# Model configuration
# Agent models
# NOTE: Gemini 3 models require global region. GlobalAdkApp preserves
# GOOGLE_CLOUD_LOCATION=global after Agent Engine setup.
# See: app/agent_engine_app.py and https://github.com/google/adk-python/issues/3628
MODEL = os.environ.get(
    "AGENT_MODEL", "gemini-3.7-flash"
)  # Main agent model (GA; global region supported)

# Media generation models — env-overridable so different model IDs can be
# configured for testing without code changes.
# VIDEO_GEN_MODEL uses Gemini Omni Flash via the Interactions API.
IMAGE_GENERATION = os.environ.get(
    "IMAGE_GENERATION_MODEL", "gemini-3-pro-image"
)  # Stage 1 scene images
VIDEO_GEN_MODEL = os.environ.get(
    "VIDEO_GEN_MODEL", "gemini-omni-flash-preview"
)  # Stage 2 video animation (Gemini Omni Flash)

# Phase 14c: Gemini Omni Flash post-generation visual-edit tool.
# Preview/experimental model with a defined sunset (Vertex model card:
# retirement 2027-06-30) -- kept fully opt-in via ENABLE_OMNI_EDIT.
OMNI_EDIT_MODEL = os.environ.get("OMNI_EDIT_MODEL", "gemini-omni-flash-preview")
ENABLE_OMNI_EDIT = (
    os.environ.get("ENABLE_OMNI_EDIT", "false").strip().lower() == "true"
)

# Video configuration
VIDEO_ASPECT_RATIO = "9:16"  # Vertical format for retail displays
VIDEO_DURATION_SECONDS = (
    4  # Default video duration (4 seconds for Gemini Omni Flash)
)
# API Keys (loaded from environment)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
# Support both GOOGLE_MAPS_API_KEY and MAPS_API_KEY (from .env)
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get(
    "MAPS_API_KEY"
)

# Cloud Run detection
IS_CLOUD_RUN = os.environ.get("K_SERVICE") is not None

# Agent Engine detection (set by Vertex AI Agent Engine runtime)
IS_AGENT_ENGINE = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID") is not None

# Combined: running in any managed cloud environment
IS_CLOUD_ENVIRONMENT = IS_CLOUD_RUN or IS_AGENT_ENGINE

# GCS configuration — explicit OPT-IN (Phase 15 local-first).
# Unset (or empty) GCS_BUCKET means local mode: all assets (product images,
# videos, thumbnails) live under LOCAL_ASSETS_DIR and tool responses carry
# no public storage URLs. Cloud deploys set GCS_BUCKET explicitly.
GCS_BUCKET = os.environ.get("GCS_BUCKET") or None

# GCS paths for assets
GCS_PRODUCT_IMAGES_PREFIX = (
    "product-images/"  # Renamed from seed-images per feedback
)
GCS_SEED_IMAGES_PREFIX = "seed-images/"  # Legacy, deprecated
GCS_GENERATED_PREFIX = "generated/"

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)

# Local asset root (Phase 15): single knob for where local-mode assets live.
# Defaults to the project root so the pre-existing selected/ and generated/
# locations are unchanged; product images join them under product-images/.
# Override with the LOCAL_ASSETS_DIR env var.
LOCAL_ASSETS_DIR = os.environ.get("LOCAL_ASSETS_DIR") or PROJECT_DIR
SELECTED_DIR = os.path.join(LOCAL_ASSETS_DIR, "selected")
GENERATED_DIR = os.path.join(LOCAL_ASSETS_DIR, "generated")
PRODUCT_IMAGES_DIR = os.path.join(LOCAL_ASSETS_DIR, "product-images")

# Database path
# - Local development: Use project root (persistent across runs)
# - Cloud Run: Use app directory (ephemeral, mock data repopulates on each container start)
# - Agent Engine: Use /tmp (ephemeral, writable in managed container)
if IS_AGENT_ENGINE:
    # Agent Engine: /tmp is guaranteed writable in the managed container
    # Database is ephemeral - mock data repopulates on each instance
    DB_PATH = "/tmp/campaigns.db"  # noqa: S108
elif IS_CLOUD_RUN:
    # In Cloud Run, the container has the app/ folder as working context
    # Use a path inside the deployed directory
    DB_PATH = os.path.join(BASE_DIR, "campaigns.db")
else:
    # Local development: use project root for persistence
    DB_PATH = os.path.join(PROJECT_DIR, "campaigns.db")

# App metadata
APP_NAME = "ad_campaign_agent"
APP_DESCRIPTION = "Retail ad campaign management agent with video generation for in-store media networks"

# Campaign categories — mirrors the CHECK constraint on campaigns.category
# in app/database/db.py (the source of truth). Keep the two in sync.
CAMPAIGN_CATEGORIES = [
    "summer",
    "formal",
    "professional",
    "essentials",
    "holiday",
    "always-on",
]

# Campaign statuses
CAMPAIGN_STATUSES = ["draft", "active", "paused", "completed"]
