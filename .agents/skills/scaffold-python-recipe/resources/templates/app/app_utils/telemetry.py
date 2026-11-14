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

from __future__ import annotations

import functools
import logging
import os
import re
import sys

RECIPE_NAME = "<RECIPE_NAME>"
_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _clean(value: str) -> str:
    return _SAFE_TOKEN_RE.sub("-", value.strip()).strip("-") or "unknown"


def is_telemetry_enabled() -> bool:
    """Return False when opted out or running inside automated tests/CI."""
    if os.environ.get("USAGE_ANALYTICS_ENABLED", "true").lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    if (
        os.environ.get("INTEGRATION_TEST")
        or os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("CI")
    ):
        return False
    return True


def detect_surface() -> str:
    """Detect the runtime hosting surface from standard platform env vars."""
    if os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        return "agent_runtime"
    if os.environ.get("K_SERVICE"):
        return "cloud_run"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "gke"
    return "local"


def detect_channel() -> str:
    """Return 'prebuilt' for Artifact Registry images, 'source' otherwise."""
    return _clean(os.environ.get("ADK_DEPLOY_CHANNEL", "source"))


def resolve_version() -> str:
    """Return the recipe version or commit SHA from AGENT_VERSION."""
    raw = (
        os.environ.get("AGENT_VERSION") or os.environ.get("COMMIT_SHA") or ""
    ).strip()
    if not raw or raw == "0.0.0":
        return "0.1.0"
    return _clean(raw[:20])


def build_tracking_headers() -> dict[str, str]:
    """Build ``user-agent`` and ``x-goog-api-client`` with a 1-segment ``gccl/`` token."""
    try:
        if not is_telemetry_enabled():
            return {}
        version = resolve_version()
        surface = detect_surface()
        channel = detect_channel()
        # 2026-10-30 parsing rule: gccl/ token must be a single segment with no '/'.
        label = f"{version}-{RECIPE_NAME}-{surface}-{channel}"
        agent = f"{label}/{label}"
        python_ver = ".".join(str(n) for n in sys.version_info[:3])
        return {
            "user-agent": agent,
            "x-goog-api-client": (
                f"{agent} gl-python/{python_ver} gccl/{label}"
            ),
        }
    except Exception:
        return {}


@functools.lru_cache(maxsize=1)
def register_adk_telemetry() -> None:
    """Fail-open hook: wrap ADK's Gemini._tracking_headers once per process."""
    try:
        from google.adk.models.google_llm import Gemini

        orig_fn = getattr(Gemini, "_tracking_headers", None)
        if orig_fn is None or getattr(orig_fn, "_adk_recipe_wrapped", False):
            return

        def _wrapped_tracking_headers(self) -> dict[str, str]:
            base = dict(orig_fn(self) or {})
            try:
                extra = build_tracking_headers()
                for key, val in extra.items():
                    existing = base.get(key, "").split()
                    for tok in val.split():
                        if tok not in existing:
                            existing.append(tok)
                    if existing:
                        base[key] = " ".join(existing)
            except Exception:
                return base
            return base

        _wrapped_tracking_headers._adk_recipe_wrapped = True  # type: ignore[attr-defined]
        Gemini._tracking_headers = _wrapped_tracking_headers  # type: ignore[method-assign]
    except Exception:
        return


def setup_telemetry() -> None:
    """Configure OpenTelemetry, GenAI telemetry with GCS upload, and usage attribution."""
    register_adk_telemetry()

    bucket = os.environ.get("LOGS_BUCKET_NAME")
    capture_content = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
    )
    if bucket and capture_content != "false":
        logging.info(
            "Prompt-response logging enabled - mode: NO_CONTENT"
            " (metadata only, no prompts/responses)"
        )
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = (
            "NO_CONTENT"
        )
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl"
        )
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload"
        )
        os.environ.setdefault(
            "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
        )
        commit_sha = os.environ.get("COMMIT_SHA", "dev")
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"service.namespace=<RECIPE_NAME>,service.version={commit_sha}",
        )
        path = os.environ.get("GENAI_TELEMETRY_PATH", "completions")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{bucket}/{path}",
        )
    else:
        logging.info(
            "Prompt-response logging disabled (set LOGS_BUCKET_NAME"
            "=gs://your-bucket and"
            " OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
            "=NO_CONTENT to enable)"
        )
