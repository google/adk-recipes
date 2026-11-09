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

# ruff: noqa: S110
"""
Tokenomics SDK - Core Configuration Manager
Supports Prototyping, Development, Testing, and Production environments.
"""

import copy
import dataclasses
import json
import math
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from adk_tokenomics_plugin.prompts import get_optimization_prompts

_BQ_PROJECT_RE = re.compile(
    r"^(?:[a-z0-9.-]+:)?[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$"
)
_BQ_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
_BQ_TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")
_BQ_TABLE_OR_VIEW_FIELDS = frozenset({"table_id", "view_id", "table", "view"})


def validate_bq_project_id(project_id: str) -> str:
    """Validates and normalizes a GCP / BigQuery project identifier."""
    if not isinstance(project_id, str):
        raise ValueError("Invalid BigQuery project_id: must be a string.")
    cleaned = project_id.strip()
    if not cleaned or not _BQ_PROJECT_RE.match(cleaned):
        raise ValueError(f"Invalid BigQuery project_id: {project_id!r}")
    return cleaned


def validate_bq_identifier(value: str, field_name: str = "identifier") -> str:
    """Validates and normalizes a BigQuery dataset, table, or view identifier."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid BigQuery {field_name}: must be a string.")
    cleaned = value.strip()
    if field_name in _BQ_TABLE_OR_VIEW_FIELDS:
        if not cleaned or "--" in cleaned or not _BQ_TABLE_RE.match(cleaned):
            raise ValueError(f"Invalid BigQuery {field_name}: {value!r}")
        return cleaned
    if not cleaned or not _BQ_IDENT_RE.match(cleaned):
        raise ValueError(f"Invalid BigQuery {field_name}: {value!r}")
    return cleaned


DEFAULT_MODELS_CONFIG: dict[str, list[dict[str, Any]]] = {
    "models": [
        {
            "id": "publishers/google/models/gemini-3.8-flash",
            "name": "Gemini 3.8 Flash",
            "provider": "google",
            "pricing": {
                "input": 0.75,
                "cached": 0.075,
                "output": 3.75,
                "input_per_million": 0.75,
                "cached_per_million": 0.075,
                "output_per_million": 3.75,
            },
            "thinking_budget": 4096,
            "max_output_tokens": 8192,
        },
        {
            "id": "publishers/google/models/gemini-4-argon",
            "name": "Gemini 4 Argon",
            "provider": "google",
            "pricing": {
                "input": 2.00,
                "cached": 0.10,
                "output": 10.00,
                "input_per_million": 2.00,
                "cached_per_million": 0.10,
                "output_per_million": 10.00,
            },
            "thinking_budget": "medium",
            "max_output_tokens": 65536,
        },
        {
            "id": "claude-3-5-haiku",
            "name": "Claude 3.5 Haiku",
            "provider": "anthropic",
            "pricing": {
                "input": 0.80,
                "cached": 0.08,
                "output": 4.00,
                "input_per_million": 0.80,
                "cached_per_million": 0.08,
                "output_per_million": 4.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "claude-3-5-sonnet-v2@20241022",
            "name": "Claude 3.5 Sonnet v2",
            "provider": "anthropic",
            "pricing": {
                "input": 3.00,
                "cached": 0.30,
                "output": 15.00,
                "input_per_million": 3.00,
                "cached_per_million": 0.30,
                "output_per_million": 15.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "claude-3-7-sonnet",
            "name": "Claude 3.7 Sonnet",
            "provider": "anthropic",
            "pricing": {
                "input": 3.00,
                "cached": 0.30,
                "output": 15.00,
                "input_per_million": 3.00,
                "cached_per_million": 0.30,
                "output_per_million": 15.00,
            },
            "thinking_budget": 4096,
            "max_output_tokens": 8192,
        },
        {
            "id": "claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "provider": "anthropic",
            "pricing": {
                "input": 3.00,
                "cached": 0.30,
                "output": 15.00,
                "input_per_million": 3.00,
                "cached_per_million": 0.30,
                "output_per_million": 15.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "claude-opus-4",
            "name": "Claude Opus 4",
            "provider": "anthropic",
            "pricing": {
                "input": 15.00,
                "cached": 1.50,
                "output": 75.00,
                "input_per_million": 15.00,
                "cached_per_million": 1.50,
                "output_per_million": 75.00,
            },
            "thinking_budget": 4096,
            "max_output_tokens": 8192,
        },
        {
            "id": "gpt-4o-mini",
            "name": "OpenAI GPT-4o Mini",
            "provider": "openai",
            "pricing": {
                "input": 0.15,
                "cached": 0.075,
                "output": 0.60,
                "input_per_million": 0.15,
                "cached_per_million": 0.075,
                "output_per_million": 0.60,
            },
            "thinking_budget": 0,
            "max_output_tokens": 4096,
        },
        {
            "id": "gpt-4o",
            "name": "OpenAI GPT-4o",
            "provider": "openai",
            "pricing": {
                "input": 2.50,
                "cached": 1.25,
                "output": 10.00,
                "input_per_million": 2.50,
                "cached_per_million": 1.25,
                "output_per_million": 10.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 4096,
        },
        {
            "id": "gpt-5",
            "name": "OpenAI GPT-5",
            "provider": "openai",
            "pricing": {
                "input": 1.25,
                "cached": 0.125,
                "output": 10.00,
                "input_per_million": 1.25,
                "cached_per_million": 0.125,
                "output_per_million": 10.00,
            },
            "thinking_budget": 4096,
            "max_output_tokens": 8192,
        },
        {
            "id": "meta/llama-3.1-8b-instruct",
            "name": "Llama 3.1 8B Instruct (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.05,
                "cached": 0.01,
                "output": 0.08,
                "input_per_million": 0.05,
                "cached_per_million": 0.01,
                "output_per_million": 0.08,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "meta/llama-3.3-70b-instruct",
            "name": "Llama 3.3 70B Instruct (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.60,
                "cached": 0.15,
                "output": 0.60,
                "input_per_million": 0.60,
                "cached_per_million": 0.15,
                "output_per_million": 0.60,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "meta/llama-3.1-405b-instruct",
            "name": "Llama 3.1 405B Instruct (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 1.80,
                "cached": 0.45,
                "output": 1.80,
                "input_per_million": 1.80,
                "cached_per_million": 0.45,
                "output_per_million": 1.80,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "deepseek/deepseek-v3",
            "name": "DeepSeek V3 (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.27,
                "cached": 0.07,
                "output": 1.10,
                "input_per_million": 0.27,
                "cached_per_million": 0.07,
                "output_per_million": 1.10,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "deepseek/deepseek-r1",
            "name": "DeepSeek R1 (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.55,
                "cached": 0.14,
                "output": 2.19,
                "input_per_million": 0.55,
                "cached_per_million": 0.14,
                "output_per_million": 2.19,
            },
            "thinking_budget": 4096,
            "max_output_tokens": 8192,
        },
        {
            "id": "mistralai/mistral-small",
            "name": "Mistral Small (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.10,
                "cached": 0.025,
                "output": 0.30,
                "input_per_million": 0.10,
                "cached_per_million": 0.025,
                "output_per_million": 0.30,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "mistralai/mistral-large",
            "name": "Mistral Large (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 2.00,
                "cached": 0.50,
                "output": 6.00,
                "input_per_million": 2.00,
                "cached_per_million": 0.50,
                "output_per_million": 6.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "qwen/qwen-2.5-72b-instruct",
            "name": "Qwen 2.5 72B Instruct (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.35,
                "cached": 0.09,
                "output": 0.40,
                "input_per_million": 0.35,
                "cached_per_million": 0.09,
                "output_per_million": 0.40,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "google/gemma-3-27b-it",
            "name": "Gemma 3 27B IT (Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.10,
                "cached": 0.025,
                "output": 0.20,
                "input_per_million": 0.10,
                "cached_per_million": 0.025,
                "output_per_million": 0.20,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
        {
            "id": "ollama/local-open-weight",
            "name": "Local Ollama / vLLM (Self-Hosted Open Weight)",
            "provider": "open_weight",
            "pricing": {
                "input": 0.00,
                "cached": 0.00,
                "output": 0.00,
                "input_per_million": 0.00,
                "cached_per_million": 0.00,
                "output_per_million": 0.00,
            },
            "thinking_budget": 0,
            "max_output_tokens": 8192,
        },
    ]
}


def _strip_google_model_prefix(model_id: str) -> str:
    low = model_id.lower()
    if low.startswith("publishers/google/models/"):
        return model_id[len("publishers/google/models/") :]
    if low.startswith("models/"):
        return model_id[len("models/") :]
    return model_id


_CANONICAL_DEFAULT_MODEL_IDS: dict[str, str] = {}
for _default_model_entry in DEFAULT_MODELS_CONFIG["models"]:
    _canon_id = str(_default_model_entry["id"]).strip()
    _CANONICAL_DEFAULT_MODEL_IDS[_canon_id.lower()] = _canon_id
    _CANONICAL_DEFAULT_MODEL_IDS[
        _strip_google_model_prefix(_canon_id).lower()
    ] = _canon_id


def canonicalize_model_id(model_id: str) -> str:
    """Strips ONLY 'publishers/google/models/' and 'models/' prefixes, returning canonical ID ONLY on exact case-insensitive match against DEFAULT_MODELS_CONFIG."""
    if not isinstance(model_id, str):
        return str(model_id or "").strip()
    cleaned = model_id.strip()
    if not cleaned:
        return ""
    low = cleaned.lower()
    if low in _CANONICAL_DEFAULT_MODEL_IDS:
        return _CANONICAL_DEFAULT_MODEL_IDS[low]
    stripped_low = _strip_google_model_prefix(cleaned).lower()
    if stripped_low in _CANONICAL_DEFAULT_MODEL_IDS:
        return _CANONICAL_DEFAULT_MODEL_IDS[stripped_low]
    return cleaned


def _read_local_dotenv() -> dict[str, str]:
    """Reads optional CWD .env values without mutating global os.environ."""
    dotenv_vars: dict[str, str] = {}
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, encoding="utf-8") as ef:
                for line in ef:
                    stripped = line.strip()
                    if (
                        stripped
                        and not stripped.startswith("#")
                        and "=" in stripped
                    ):
                        k, v = stripped.split("=", 1)
                        dotenv_vars.setdefault(
                            k.strip(), v.strip().strip('"').strip("'")
                        )
        except Exception:
            pass
    return dotenv_vars


def resolve_env_bq_identifiers(
    dataset_id: str | None = None,
    table_id: str | None = None,
    view_id: str | None = None,
    *,
    include_dotenv: bool = True,
) -> tuple[str, str, str]:
    """Resolves BigQuery (dataset_id, table_id, view_id) via explicit args -> BQ_ANALYTICS_* -> BIGQUERY_*_ID -> BIGQUERY_* -> BQ_*_ID -> defaults."""
    dotenv_vars = _read_local_dotenv() if include_dotenv else {}

    def _lookup_env(*keys: str, default: str) -> str:
        for key in keys:
            env_val = os.environ.get(key)
            if env_val is not None and env_val.strip():
                return env_val.strip()
            dotenv_val = dotenv_vars.get(key)
            if dotenv_val is not None and dotenv_val.strip():
                return dotenv_val.strip()
        return default

    resolved_dataset = (
        str(dataset_id).strip()
        if (dataset_id is not None and str(dataset_id).strip())
        else _lookup_env(
            "BQ_ANALYTICS_DATASET_ID",
            "BIGQUERY_DATASET_ID",
            "BIGQUERY_DATASET",
            "BQ_DATASET_ID",
            default="bq_adk_ds",
        )
    )
    resolved_table = (
        str(table_id).strip()
        if (table_id is not None and str(table_id).strip())
        else _lookup_env(
            "BQ_ANALYTICS_TABLE_ID",
            "BIGQUERY_TABLE_ID",
            "BIGQUERY_TABLE",
            "BQ_TABLE_ID",
            default="adk_agent_events",
        )
    )
    resolved_view = (
        str(view_id).strip()
        if (view_id is not None and str(view_id).strip())
        else _lookup_env(
            "BQ_ANALYTICS_VIEW_ID",
            "BIGQUERY_VIEW_ID",
            "BIGQUERY_VIEW",
            "BQ_VIEW_ID",
            default="v_llm_response",
        )
    )
    return resolved_dataset, resolved_table, resolved_view


@dataclass
class OptimizationRulesConfig:
    """User-customizable heuristic thresholds and parameters for ADK optimization recommendations."""

    min_cache_tokens: int = 1024
    cache_ttl_seconds: int = 300
    compaction_turn_threshold: int = 4
    compaction_growth_delta: int = 350
    compaction_interval: int = 4
    compaction_overlap: int = 1
    modular_skills_token_threshold: int = 5000
    modular_skills_reduction_ratio: float = 0.60
    max_thinking_to_output_ratio: float = 0.80
    recommended_thinking_budget: int = 256
    enable_model_tiering_hint: bool = True
    lightweight_turn_max_output_tokens: int = 600
    recommended_lite_model: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validates heuristic rule types and numerical bounds."""
        nonneg_int_fields = (
            "min_cache_tokens",
            "cache_ttl_seconds",
            "compaction_growth_delta",
            "compaction_overlap",
            "modular_skills_token_threshold",
            "recommended_thinking_budget",
            "lightweight_turn_max_output_tokens",
        )
        pos_int_fields = ("compaction_turn_threshold", "compaction_interval")
        for fname in nonneg_int_fields:
            val = getattr(self, fname)
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise ValueError(
                    f"OptimizationRulesConfig.{fname} must be a non-negative integer, got {val!r}."
                )
        for fname in pos_int_fields:
            val = getattr(self, fname)
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                raise ValueError(
                    f"OptimizationRulesConfig.{fname} must be a positive integer (>= 1), got {val!r}."
                )

        red_ratio = self.modular_skills_reduction_ratio
        if (
            isinstance(red_ratio, bool)
            or not isinstance(red_ratio, (int, float))
            or not math.isfinite(float(red_ratio))
            or not (0.0 <= float(red_ratio) <= 1.0)
        ):
            raise ValueError(
                f"OptimizationRulesConfig.modular_skills_reduction_ratio must be a finite float in [0.0, 1.0], got {red_ratio!r}."
            )
        self.modular_skills_reduction_ratio = float(red_ratio)

        think_ratio = self.max_thinking_to_output_ratio
        if (
            isinstance(think_ratio, bool)
            or not isinstance(think_ratio, (int, float))
            or not math.isfinite(float(think_ratio))
            or float(think_ratio) < 0.0
        ):
            raise ValueError(
                f"OptimizationRulesConfig.max_thinking_to_output_ratio must be a finite non-negative float, got {think_ratio!r}."
            )
        self.max_thinking_to_output_ratio = float(think_ratio)

        if not isinstance(self.enable_model_tiering_hint, bool):
            raise ValueError(
                f"OptimizationRulesConfig.enable_model_tiering_hint must be a bool, got {self.enable_model_tiering_hint!r}."
            )
        if self.recommended_lite_model is not None:
            if (
                not isinstance(self.recommended_lite_model, str)
                or not self.recommended_lite_model.strip()
            ):
                raise ValueError(
                    f"OptimizationRulesConfig.recommended_lite_model must be None or a non-empty string, got {self.recommended_lite_model!r}."
                )
            self.recommended_lite_model = self.recommended_lite_model.strip()


_VALID_RULE_KEYS = frozenset(
    f.name for f in dataclasses.fields(OptimizationRulesConfig)
)


@dataclass
class TokenomicsConfig:
    """Central configuration for Tokenomics SDK."""

    mode: str | None = None  # "prototype", "dev", "test", "prod"
    sink: str | None = None  # "memory", "bigquery"
    project_id: str | None = None
    dataset_id: str | None = None
    table_id: str | None = None
    view_id: str | None = None
    default_model: str | None = None
    models_config_path: str | None = None
    prompts_config_path: str | None = None
    legacy_flat_schema: bool = False
    async_streaming: bool = True
    batch_size: int = 10
    flush_interval_seconds: float = 2.0
    max_history_turns: int = 100000
    custom_pricing: dict[str, dict[str, Any]] = field(default_factory=dict)
    optimization_rules: OptimizationRulesConfig = field(
        default_factory=OptimizationRulesConfig
    )
    optimization_prompts: dict[str, str] = field(default_factory=dict)
    custom_recommendation_callbacks: list[
        Callable[[dict[str, Any]], str | None]
    ] = field(default_factory=list)

    def __post_init__(self):
        dotenv_vars = _read_local_dotenv()

        def _cfg_env(key: str) -> str | None:
            val = os.environ.get(key)
            if val is None:
                val = dotenv_vars.get(key)
            return val

        if isinstance(self.optimization_rules, dict):
            unknown_keys = sorted(
                set(self.optimization_rules.keys()) - _VALID_RULE_KEYS
            )
            if unknown_keys:
                raise ValueError(
                    f"Unknown optimization rule key(s): {', '.join(unknown_keys)}"
                )
            self.optimization_rules = OptimizationRulesConfig(
                **self.optimization_rules
            )
        elif isinstance(self.optimization_rules, OptimizationRulesConfig):
            self.optimization_rules.validate()
        else:
            raise ValueError(
                f"optimization_rules must be an OptimizationRulesConfig or dict, got {type(self.optimization_rules).__name__}."
            )

        if self.prompts_config_path is not None:
            raw_prompts_path = str(self.prompts_config_path).strip()
            if not raw_prompts_path or not os.path.exists(raw_prompts_path):
                raise ValueError(
                    f"prompts_config_path does not exist: {self.prompts_config_path!r}"
                )
            try:
                with open(raw_prompts_path, encoding="utf-8") as pf:
                    loaded_prompts = json.load(pf)
            except Exception as exc:
                raise ValueError(
                    f"Failed to parse JSON from prompts_config_path {raw_prompts_path!r}: {exc}"
                ) from exc
            if not isinstance(loaded_prompts, dict):
                raise ValueError(
                    f"prompts_config_path {raw_prompts_path!r} must contain a JSON object (dict), "
                    f"got {type(loaded_prompts).__name__}."
                )
            if "prompts" in loaded_prompts:
                if not isinstance(loaded_prompts["prompts"], dict):
                    raise ValueError(
                        f"'prompts' section in {raw_prompts_path!r} must be a JSON object (dict)."
                    )
                self.optimization_prompts = {
                    **loaded_prompts["prompts"],
                    **self.optimization_prompts,
                }
            if "rules" in loaded_prompts:
                if not isinstance(loaded_prompts["rules"], dict):
                    raise ValueError(
                        f"'rules' section in {raw_prompts_path!r} must be a JSON object (dict)."
                    )
                unknown_rule_keys = sorted(
                    set(loaded_prompts["rules"].keys()) - _VALID_RULE_KEYS
                )
                if unknown_rule_keys:
                    raise ValueError(
                        f"Unknown optimization rule key(s) in {raw_prompts_path!r}: {', '.join(unknown_rule_keys)}"
                    )
                for rk, rv in loaded_prompts["rules"].items():
                    setattr(self.optimization_rules, rk, rv)
                self.optimization_rules.validate()
            if (
                "prompts" not in loaded_prompts
                and "rules" not in loaded_prompts
            ):
                self.optimization_prompts = {
                    **loaded_prompts,
                    **self.optimization_prompts,
                }

        if not self.project_id:
            self.project_id = (
                _cfg_env("GOOGLE_CLOUD_PROJECT")
                or _cfg_env("GCP_PROJECT")
                or _cfg_env("BIGQUERY_PROJECT_ID")
                or None
            )

        self.dataset_id, self.table_id, self.view_id = (
            resolve_env_bq_identifiers(
                dataset_id=self.dataset_id,
                table_id=self.table_id,
                view_id=self.view_id,
                include_dotenv=True,
            )
        )
        self.legacy_flat_schema = bool(self.legacy_flat_schema)

        if self.mode is not None and str(self.mode).strip():
            self.mode = str(self.mode).strip().lower()
        else:
            env_mode = _cfg_env("TOKENOMICS_MODE")
            self.mode = (
                env_mode.strip().lower()
                if (env_mode and env_mode.strip())
                else "dev"
            )

        if self.sink is not None and str(self.sink).strip():
            self.sink = str(self.sink).strip().lower()
        else:
            env_sink = _cfg_env("TOKENOMICS_SINK")
            if env_sink and env_sink.strip():
                self.sink = env_sink.strip().lower()
            else:
                self.sink = "bigquery" if self.mode == "prod" else "memory"

        if self.default_model is not None and str(self.default_model).strip():
            self.default_model = str(self.default_model).strip()
        else:
            env_default_model = _cfg_env("TOKENOMICS_DEFAULT_MODEL")
            self.default_model = (
                env_default_model.strip()
                if (env_default_model and env_default_model.strip())
                else "publishers/google/models/gemini-3.8-flash"
            )

        if not self.models_config_path:
            env_models_cfg = _cfg_env(
                "TOKENOMICS_MODELS_CONFIG_PATH"
            ) or _cfg_env("TOKENOMICS_MODELS_CONFIG")
            if env_models_cfg and env_models_cfg.strip():
                self.models_config_path = env_models_cfg.strip()

    def register_model(
        self,
        model_id: str,
        input_per_million: float,
        output_per_million: float,
        cached_per_million: float | None = None,
        provider: str = "custom",
        name: str | None = None,
        display_name: str | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Registers or overrides pricing for any 1P, 3P (Claude, OpenAI), or Open-Weight (Llama, DeepSeek, Mistral) model."""
        canon_id = canonicalize_model_id(model_id)
        cache_rate = (
            cached_per_million
            if cached_per_million is not None
            else round(input_per_million * 0.25, 6)
        )
        entry = {
            "name": name or display_name or canon_id,
            "provider": provider,
            "tier": tier or "Custom",
            "input": float(input_per_million),
            "cached": float(cache_rate),
            "output": float(output_per_million),
            "input_per_million": float(input_per_million),
            "cached_per_million": float(cache_rate),
            "output_per_million": float(output_per_million),
        }
        if canon_id != model_id and model_id in self.custom_pricing:
            self.custom_pricing.pop(model_id, None)
        self.custom_pricing[canon_id] = entry
        return entry

    def get_optimization_prompts(self) -> dict[str, str]:
        """Returns the merged optimization recommendation prompt templates."""
        return get_optimization_prompts(self.optimization_prompts)

    @staticmethod
    def _normalize_loaded_model_dict(
        m: dict[str, Any], base_entry: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        canon_id = canonicalize_model_id(str(m.get("id") or ""))
        base_p = dict((base_entry or {}).get("pricing", {}))
        mp = dict(m.get("pricing") or {})
        in_val = (
            mp["input"]
            if "input" in mp
            else mp.get(
                "input_per_million",
                base_p.get("input_per_million", base_p.get("input", 0.30)),
            )
        )
        cache_val = (
            mp["cached"]
            if "cached" in mp
            else mp.get(
                "cached_per_million",
                base_p.get("cached_per_million", base_p.get("cached", 0.075)),
            )
        )
        out_val = (
            mp["output"]
            if "output" in mp
            else mp.get(
                "output_per_million",
                base_p.get("output_per_million", base_p.get("output", 1.20)),
            )
        )
        p = {**base_p, **mp}
        p.update(
            {
                "input": in_val,
                "cached": cache_val,
                "output": out_val,
                "input_per_million": in_val,
                "cached_per_million": cache_val,
                "output_per_million": out_val,
            }
        )
        entry = dict(base_entry) if base_entry else {}
        entry.update(m)
        entry["id"] = canon_id
        entry["pricing"] = p
        return entry

    def get_models_config(self) -> dict[str, Any]:
        """Loads models configuration from the active file, applying canonicalization, deleted_ids, replace_defaults, and custom_pricing."""
        resolved_path = resolve_models_config_path(self.models_config_path)
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pkg_cfg = os.path.join(pkg_root, "models_config.json")

        loaded: dict[str, Any] | None = None
        if resolved_path and os.path.exists(resolved_path):
            try:
                with open(resolved_path, encoding="utf-8") as f:
                    parsed = json.load(f)
                if isinstance(parsed, dict):
                    loaded = parsed
            except Exception:
                loaded = None

        replace_defaults = (
            bool(loaded.get("replace_defaults"))
            if isinstance(loaded, dict)
            else False
        )
        merged_by_id: dict[str, dict[str, Any]] = {}

        if not replace_defaults:
            for dm in DEFAULT_MODELS_CONFIG["models"]:
                dm_copy = copy.deepcopy(dm)
                merged_by_id[dm_copy["id"]] = dm_copy
            if os.path.exists(pkg_cfg) and (
                not resolved_path
                or os.path.abspath(resolved_path) != os.path.abspath(pkg_cfg)
                or loaded is None
            ):
                try:
                    with open(pkg_cfg, encoding="utf-8") as pf:
                        pkg_loaded = json.load(pf)
                    if isinstance(pkg_loaded, dict):
                        for pm in pkg_loaded.get("models", []):
                            if (
                                isinstance(pm, dict)
                                and str(pm.get("id") or "").strip()
                            ):
                                pm_id = canonicalize_model_id(str(pm["id"]))
                                merged_by_id[pm_id] = (
                                    self._normalize_loaded_model_dict(
                                        pm, merged_by_id.get(pm_id)
                                    )
                                )
                except Exception:
                    pass

            # Suppress ONLY seeded defaults listed in deleted_ids (never entries present in user file 'models')
            raw_deleted = (
                loaded.get("deleted_ids") if isinstance(loaded, dict) else None
            )
            if isinstance(raw_deleted, list):
                for raw_did in raw_deleted:
                    if isinstance(raw_did, str) and raw_did.strip():
                        did_clean = raw_did.strip()
                        did_canon = canonicalize_model_id(did_clean)
                        merged_by_id.pop(did_clean, None)
                        merged_by_id.pop(did_canon, None)

        if isinstance(loaded, dict):
            raw_models = loaded.get("models")
            if isinstance(raw_models, list):
                for m in raw_models:
                    if isinstance(m, dict) and str(m.get("id") or "").strip():
                        m_id = canonicalize_model_id(str(m["id"]))
                        merged_by_id[m_id] = self._normalize_loaded_model_dict(
                            m, merged_by_id.get(m_id)
                        )

        for raw_custom_id, custom_p in (self.custom_pricing or {}).items():
            custom_id = canonicalize_model_id(str(raw_custom_id))
            existing_entry = merged_by_id.get(custom_id) or {}
            in_val = float(
                custom_p.get("input_per_million", custom_p.get("input", 0.30))
            )
            cache_val = float(
                custom_p.get(
                    "cached_per_million", custom_p.get("cached", in_val * 0.25)
                )
            )
            out_val = float(
                custom_p.get("output_per_million", custom_p.get("output", 1.20))
            )
            raw_name = custom_p.get("name")
            resolved_name = (
                raw_name
                if (raw_name and raw_name not in (custom_id, raw_custom_id))
                else (existing_entry.get("name") or raw_name or custom_id)
            )
            updated_entry = dict(existing_entry)
            updated_entry.update(
                {
                    "id": custom_id,
                    "name": resolved_name,
                    "provider": custom_p.get(
                        "provider", existing_entry.get("provider", "custom")
                    ),
                    "pricing": {
                        "input": in_val,
                        "cached": cache_val,
                        "output": out_val,
                        "input_per_million": in_val,
                        "cached_per_million": cache_val,
                        "output_per_million": out_val,
                    },
                }
            )
            merged_by_id[custom_id] = updated_entry
        return {"models": list(merged_by_id.values())}


def _get_user_models_config_path() -> str:
    raw_xdg = os.environ.get("XDG_CONFIG_HOME")
    xdg_base = (
        raw_xdg.strip()
        if raw_xdg and raw_xdg.strip()
        else os.path.expanduser("~/.config")
    )
    return os.path.join(xdg_base, "adk-tokenomics", "models_config.json")


def _get_env_models_config_override() -> str:
    if "TOKENOMICS_MODELS_CONFIG_PATH" in os.environ:
        raw_alt = os.environ.get("TOKENOMICS_MODELS_CONFIG")
        return os.environ["TOKENOMICS_MODELS_CONFIG_PATH"].strip() or (
            raw_alt.strip() if raw_alt else ""
        )
    if "TOKENOMICS_MODELS_CONFIG" in os.environ:
        return os.environ["TOKENOMICS_MODELS_CONFIG"].strip()
    dotenv_vars = _read_local_dotenv()
    return (
        dotenv_vars.get("TOKENOMICS_MODELS_CONFIG_PATH", "").strip()
        or dotenv_vars.get("TOKENOMICS_MODELS_CONFIG", "").strip()
    )


def resolve_models_config_path(explicit_path: str | None = None) -> str:
    """Resolves the active models_config.json path for reading (explicit -> env -> test WEB_DIR -> cwd -> user XDG -> packaged file)."""
    env_override = _get_env_models_config_override()
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    if env_override and os.path.exists(env_override):
        return env_override

    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg_cfg = os.path.join(pkg_root, "models_config.json")
    default_web_dir = os.path.join(pkg_root, "web")
    cli_web_mod = sys.modules.get("adk_tokenomics_plugin.cli_web")
    active_web_dir = (
        getattr(cli_web_mod, "WEB_DIR", default_web_dir)
        if cli_web_mod
        else default_web_dir
    )

    # Support isolated unit tests that patch cli_web.WEB_DIR to a temporary directory
    if os.path.abspath(active_web_dir) != os.path.abspath(default_web_dir):
        test_cfg = os.path.join(active_web_dir, "models_config.json")
        if os.path.exists(test_cfg):
            return test_cfg

    # Support optional project-local override in current working directory (distinct from packaged file)
    cwd_cfg = os.path.join(os.getcwd(), "models_config.json")
    if os.path.exists(cwd_cfg) and os.path.abspath(cwd_cfg) != os.path.abspath(
        pkg_cfg
    ):
        return cwd_cfg

    # Support user-level XDG override written by dashboard/CLI
    user_cfg = _get_user_models_config_path()
    if os.path.exists(user_cfg):
        return user_cfg

    # Single canonical packaged models_config.json
    return pkg_cfg


def resolve_writable_models_config_path(
    explicit_path: str | None = None,
) -> str:
    """Resolves a writable models_config.json path so packaged adk_tokenomics_plugin/models_config.json is never mutated."""
    if explicit_path and str(explicit_path).strip():
        target = str(explicit_path).strip()
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return target

    env_override = _get_env_models_config_override()
    if env_override:
        parent = os.path.dirname(os.path.abspath(env_override))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return env_override

    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg_cfg = os.path.join(pkg_root, "models_config.json")
    default_web_dir = os.path.join(pkg_root, "web")
    cli_web_mod = sys.modules.get("adk_tokenomics_plugin.cli_web")
    active_web_dir = (
        getattr(cli_web_mod, "WEB_DIR", default_web_dir)
        if cli_web_mod
        else default_web_dir
    )

    if os.path.abspath(active_web_dir) != os.path.abspath(default_web_dir):
        os.makedirs(active_web_dir, exist_ok=True)
        return os.path.join(active_web_dir, "models_config.json")

    cwd_cfg = os.path.join(os.getcwd(), "models_config.json")
    if os.path.exists(cwd_cfg) and os.path.abspath(cwd_cfg) != os.path.abspath(
        pkg_cfg
    ):
        return cwd_cfg

    user_cfg = _get_user_models_config_path()
    os.makedirs(os.path.dirname(user_cfg), exist_ok=True)
    return user_cfg
