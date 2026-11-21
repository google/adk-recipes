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

import logging
import os
import re
import sys
import threading

RECIPE_NAME = "<RECIPE_NAME>"
_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_PLACEHOLDER_RE = re.compile(r"^<[^<>]+>$")
_LOCK = threading.Lock()


def _clean(value: str) -> str:
    return _SAFE_TOKEN_RE.sub("-", value.strip()).strip("-") or "unknown"


def _env(name: str, default: str = "") -> str:
    """Read an env var, ignoring `<TODO...>` placeholder strings."""
    val = os.environ.get(name, "").strip()
    if not val or _PLACEHOLDER_RE.match(val):
        return default
    return val


def is_telemetry_enabled() -> bool:
    """Return False when opted out or running inside automated tests/CI."""
    if _env("USAGE_ANALYTICS_ENABLED", "true").lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    if _env("INTEGRATION_TEST") or _env("PYTEST_CURRENT_TEST") or _env("CI"):
        return False
    return True


def detect_surface() -> str:
    """Detect the runtime hosting surface from standard platform env vars."""
    if _env("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        return "agent_runtime"
    if _env("K_SERVICE"):
        return "cloud_run"
    if _env("KUBERNETES_SERVICE_HOST"):
        return "gke"
    return "local"


def detect_channel() -> str:
    """Return 'prebuilt' for Artifact Registry images, 'source' otherwise."""
    return _clean(_env("ADK_DEPLOY_CHANNEL", "source"))


def resolve_version() -> str:
    """Return the recipe version or commit SHA from AGENT_VERSION / COMMIT_SHA."""
    raw = _env("AGENT_VERSION") or _env("COMMIT_SHA")
    if not raw or raw == "0.0.0":
        return "0.1.0"
    return _clean(raw[:32])


def build_tracking_headers() -> dict[str, str]:
    """Build ``user-agent`` and ``x-goog-api-client`` with a 1-segment ``gccl/`` token."""
    try:
        if not is_telemetry_enabled():
            return {}
        version = resolve_version()
        recipe = _clean(RECIPE_NAME)
        surface = detect_surface()
        channel = detect_channel()
        # 2026-10-30 parsing rule: gccl/ token must be a single segment with no '/'.
        label = f"{version}-{recipe}-{surface}-{channel}"
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


def _merge_into_headers(base: dict[str, str] | None) -> dict[str, str]:
    """Merge ``build_tracking_headers()`` into ``base`` without duplicating tokens."""
    try:
        out = dict(base or {})
    except Exception:
        return {}
    try:
        extra = build_tracking_headers()
        if not extra:
            return out
        key_lookup = {k.lower(): k for k in out}
        for key, val in extra.items():
            actual_key = key_lookup.get(key.lower(), key)
            existing = (out.get(actual_key) or "").split()
            for tok in val.split():
                if tok not in existing:
                    existing.append(tok)
            if existing:
                out[actual_key] = " ".join(existing)
    except Exception:
        return out
    return out


def register_adk_telemetry() -> None:
    """Fail-open hook: wrap ADK tracking header builders once per process."""
    with _LOCK:
        try:
            from google.adk.utils import _google_client_headers as gch

            orig_get = getattr(gch, "get_tracking_headers", None)
            if callable(orig_get) and not getattr(
                orig_get, "_adk_recipe_wrapped", False
            ):

                def _wrapped_get_tracking_headers(
                    *args: object, **kwargs: object
                ) -> dict[str, str]:
                    return _merge_into_headers(orig_get(*args, **kwargs))

                _wrapped_get_tracking_headers._adk_recipe_wrapped = True  # type: ignore[attr-defined]
                gch.get_tracking_headers = _wrapped_get_tracking_headers
        except Exception:  # noqa: S110
            pass

        try:
            from google.adk.models import google_llm
            from google.adk.models.google_llm import Gemini

            try:
                from google.adk.utils import _google_client_headers as gch

                gch_get = getattr(gch, "get_tracking_headers", None)
                if callable(gch_get) and hasattr(
                    google_llm, "get_tracking_headers"
                ):
                    google_llm.get_tracking_headers = gch_get
            except Exception:  # noqa: S110
                pass

            orig_fn = getattr(Gemini, "_tracking_headers", None)
            if callable(orig_fn) and not getattr(
                orig_fn, "_adk_recipe_wrapped", False
            ):

                def _wrapped_tracking_headers(
                    self: object, *args: object, **kwargs: object
                ) -> dict[str, str]:
                    return _merge_into_headers(orig_fn(self, *args, **kwargs))

                _wrapped_tracking_headers._adk_recipe_wrapped = True  # type: ignore[attr-defined]
                Gemini._tracking_headers = _wrapped_tracking_headers  # type: ignore[method-assign]

            orig_merge = getattr(Gemini, "_merge_tracking_headers", None)
            if callable(orig_merge) and not getattr(
                orig_merge, "_adk_recipe_wrapped", False
            ):

                def _wrapped_merge_tracking_headers(
                    self: object, *args: object, **kwargs: object
                ) -> dict[str, str]:
                    return _merge_into_headers(
                        orig_merge(self, *args, **kwargs)
                    )

                _wrapped_merge_tracking_headers._adk_recipe_wrapped = True  # type: ignore[attr-defined]
                Gemini._merge_tracking_headers = (  # type: ignore[method-assign]
                    _wrapped_merge_tracking_headers
                )
        except Exception:
            return


def setup_telemetry() -> None:
    """Configure OpenTelemetry, GenAI telemetry with GCS upload, and usage attribution."""
    register_adk_telemetry()

    bucket = _env("LOGS_BUCKET_NAME")
    capture_content = _env(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
    )
    if bucket and capture_content.lower() not in ("0", "false", "no", "off"):
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
        commit_sha = _env("COMMIT_SHA", "dev")
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"service.namespace=<RECIPE_NAME>,service.version={commit_sha}",
        )
        path = _env("GENAI_TELEMETRY_PATH", "completions")
        bucket_name = bucket.removeprefix("gs://").strip("/")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{bucket_name}/{path}",
        )
    else:
        logging.info(
            "Prompt-response logging disabled (set LOGS_BUCKET_NAME"
            "=your-bucket and"
            " OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
            "=NO_CONTENT to enable)"
        )
