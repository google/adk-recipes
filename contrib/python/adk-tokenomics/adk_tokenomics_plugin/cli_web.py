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

# ruff: noqa: RUF012, S104, S110, S310, S608
"""
ADK Token Control Tower - Embedded Web Server Engine
Serves the local interactive visual dashboard and proxies live BigQuery / trace telemetry.
"""

import copy
import glob
import html
import http.server
import ipaddress
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlparse

from adk_tokenomics_plugin.core.config import (
    DEFAULT_MODELS_CONFIG,
    TokenomicsConfig,
    canonicalize_model_id,
    resolve_env_bq_identifiers,
    resolve_models_config_path,
    resolve_writable_models_config_path,
    validate_bq_identifier,
    validate_bq_project_id,
)
from adk_tokenomics_plugin.core.pricing import (
    PricingEngine,
    _safe_nonneg_int,
    normalize_turn_tokens,
)

logger = logging.getLogger(__name__)

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGED_MODELS_CFG = os.path.join(_PKG_DIR, "models_config.json")
_DEFAULT_WEB_DIR = os.path.join(_PKG_DIR, "web")
WEB_DIR = _DEFAULT_WEB_DIR

_MODELS_CONFIG_LOCK = threading.RLock()
_BQ_CACHE_LOCK = threading.Lock()
_BQ_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
BQ_CACHE_TTL_SECONDS: float = 10.0


def _split_top_level_csv(raw_str: str) -> list[str]:
    """Splits a comma-delimited tool/skill string only at top-level depth (outside parens/brackets/quotes)."""
    text = str(raw_str or "").strip()
    if not text:
        return []
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quote_char: str | None = None
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if quote_char is not None:
            buf.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = None
            continue
        if ch in ("'", '"'):
            quote_char = ch
            buf.append(ch)
        elif ch in ("(", "[", "{"):
            depth += 1
            buf.append(ch)
        elif ch in (")", "]", "}"):
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                items.append(piece)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


_HOST_LABEL_RE = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_VALID_HOSTNAME_RE = re.compile(rf"^{_HOST_LABEL_RE}(?:\.{_HOST_LABEL_RE})*$")
_FORBIDDEN_HOST_CHARS = frozenset(" \t\r\n/\\?#@")
_LEADING_HTTP_CODE_RE = re.compile(r"^\s*(\d{3})\b")


def parse_allowed_host_pattern(
    raw_pattern: str,
) -> tuple[str, str, int | None]:
    """
    Validates and normalizes an allowed-host entry from --allowed-host or $TOKENOMICS_ALLOWED_HOSTS.
    Returns (kind, target, port) where kind is 'exact' or 'wildcard':
      - 'exact': target is lowercase hostname/IPv4 or '[ipv6]', port is None or 1..65535
      - 'wildcard': target is '.<base_domain>' (from '*.<base_domain>' or '.<base_domain>'), port is None or 1..65535
    """
    if not isinstance(raw_pattern, str):
        raise ValueError(
            f"Invalid allowed-host entry {raw_pattern!r}: expected string."
        )
    pat = raw_pattern.strip().lower()
    if not pat:
        raise ValueError("Invalid allowed-host entry: empty string.")
    if "://" in pat or any(
        ch in _FORBIDDEN_HOST_CHARS or ord(ch) < 32 for ch in pat
    ):
        raise ValueError(
            f"Invalid allowed-host entry {raw_pattern!r}: specify host or host:port without URL scheme or path."
        )
    if pat == "*":
        raise ValueError(
            "Wildcard '*' is not allowed in --allowed-host / TOKENOMICS_ALLOWED_HOSTS; use '*.<domain>'."
        )

    if pat.startswith("["):
        if "]" not in pat:
            raise ValueError(
                f"Malformed IPv6 allowed-host entry {raw_pattern!r}."
            )
        h_inner, rest = pat[1:].split("]", 1)
        try:
            ip_obj = ipaddress.IPv6Address(h_inner)
        except ValueError as exc:
            raise ValueError(
                f"Invalid IPv6 address in allowed-host entry {raw_pattern!r}."
            ) from exc
        if rest == "":
            rule_port: int | None = None
        elif rest.startswith(":") and rest[1:].isdigit():
            rule_port = int(rest[1:])
            if not (1 <= rule_port <= 65535):
                raise ValueError(
                    f"Out-of-range port in allowed-host entry {raw_pattern!r}."
                )
        else:
            raise ValueError(
                f"Invalid port suffix in allowed-host entry {raw_pattern!r}."
            )
        return ("exact", f"[{ip_obj.compressed}]", rule_port)

    if pat.count(":") >= 2:
        try:
            ip_obj = ipaddress.IPv6Address(pat)
        except ValueError as exc:
            raise ValueError(
                f"Invalid allowed-host entry {raw_pattern!r}."
            ) from exc
        return ("exact", f"[{ip_obj.compressed}]", None)

    if ":" in pat:
        h_pat, p_str = pat.rsplit(":", 1)
        if not p_str.isdigit():
            raise ValueError(
                f"Invalid port in allowed-host entry {raw_pattern!r}."
            )
        rule_port = int(p_str)
        if not (1 <= rule_port <= 65535):
            raise ValueError(
                f"Out-of-range port in allowed-host entry {raw_pattern!r}."
            )
    else:
        h_pat = pat
        rule_port = None

    if h_pat.startswith("*.") or h_pat.startswith("."):
        base = h_pat[2:] if h_pat.startswith("*.") else h_pat[1:]
        if (
            not base
            or len(base) > 253
            or not _VALID_HOSTNAME_RE.match(base)
            or all(lbl.isdigit() for lbl in base.split("."))
        ):
            raise ValueError(
                f"Invalid wildcard allowed-host entry {raw_pattern!r}."
            )
        return ("wildcard", "." + base, rule_port)

    if not h_pat or len(h_pat) > 253 or not _VALID_HOSTNAME_RE.match(h_pat):
        raise ValueError(f"Invalid allowed-host entry {raw_pattern!r}.")
    return ("exact", h_pat, rule_port)


def validate_allowed_hosts(entries: Sequence[str] | None) -> tuple[str, ...]:
    """Validates a sequence of allowed-host CLI/config strings (supporting comma-separated items)."""
    if not entries:
        return ()
    normalized: list[str] = []
    for raw_item in entries:
        if raw_item is None:
            continue
        for token in str(raw_item).split(","):
            tok = token.strip()
            if not tok:
                continue
            kind, target, rule_port = parse_allowed_host_pattern(tok)
            host_repr = f"*{target}" if kind == "wildcard" else target
            canonical = (
                f"{host_repr}:{rule_port}"
                if rule_port is not None
                else host_repr
            )
            if canonical not in normalized:
                normalized.append(canonical)
    return tuple(normalized)


def _parse_host_header(
    raw_host: str,
) -> tuple[str | None, int | None, str | None]:
    """Parses HTTP Host header into (normalized_host_part, port_or_none, error_message)."""
    cleaned = (raw_host or "").strip()
    if not cleaned:
        return None, None, "Missing Host header."
    if any(ch in _FORBIDDEN_HOST_CHARS or ord(ch) < 32 for ch in cleaned):
        return None, None, "Malformed Host header."

    if cleaned.startswith("["):
        if "]" not in cleaned:
            return None, None, "Malformed Host header."
        h_inner, rest = cleaned[1:].split("]", 1)
        try:
            ip_obj = ipaddress.IPv6Address(h_inner)
        except ValueError:
            return None, None, f"Forbidden Host header: {cleaned!r}"
        host_part = f"[{ip_obj.compressed}]"
        if rest == "":
            port_part: int | None = None
        elif rest.startswith(":"):
            p_str = rest[1:]
            if not p_str.isdigit():
                return None, None, "Invalid port in Host header."
            port_part = int(p_str)
            if not (1 <= port_part <= 65535):
                return None, None, "Invalid port in Host header."
        else:
            return None, None, "Malformed Host header."
        return host_part, port_part, None

    if cleaned.count(":") > 1:
        return None, None, "Malformed Host header."

    if ":" in cleaned:
        h_part, p_str = cleaned.rsplit(":", 1)
        if not p_str.isdigit():
            return None, None, "Invalid port in Host header."
        port_part = int(p_str)
        if not (1 <= port_part <= 65535):
            return None, None, "Invalid port in Host header."
        host_part = h_part.lower()
    else:
        host_part = cleaned.lower()
        port_part = None

    if (
        not host_part
        or len(host_part) > 253
        or not _VALID_HOSTNAME_RE.match(host_part)
    ):
        return None, None, f"Forbidden Host header: {cleaned!r}"
    return host_part, port_part, None


def get_models_config_path() -> str:
    """Returns the active models_config.json path for reading (supports WEB_DIR test overrides and user overrides)."""
    if os.path.abspath(WEB_DIR) != os.path.abspath(_DEFAULT_WEB_DIR):
        test_cfg = os.path.join(WEB_DIR, "models_config.json")
        if os.path.exists(test_cfg):
            return test_cfg
    return resolve_models_config_path()


def get_writable_models_config_path() -> str:
    """Returns a writable models_config.json path (never the packaged adk_tokenomics_plugin/models_config.json)."""
    if os.path.abspath(WEB_DIR) != os.path.abspath(_DEFAULT_WEB_DIR):
        os.makedirs(WEB_DIR, exist_ok=True)
        return os.path.join(WEB_DIR, "models_config.json")
    return resolve_writable_models_config_path()


PRICING_MODELS: dict[str, dict[str, float]] = {
    str(m["name"]): {
        "prompt": float(m["pricing"]["input"]) / 1_000_000.0,
        "cached": float(m["pricing"]["cached"]) / 1_000_000.0,
        "output": float(m["pricing"]["output"]) / 1_000_000.0,
    }
    for m in DEFAULT_MODELS_CONFIG.get("models", [])
    if isinstance(m, dict)
    and m.get("name")
    and isinstance(m.get("pricing"), dict)
}


class TokenomicsWebHandler(http.server.SimpleHTTPRequestHandler):
    timeout = 10
    MAX_REQUEST_BODY_BYTES = 1_048_576

    project_id: str = ""
    dataset_id: str = "bq_adk_ds"
    table_id: str = "adk_agent_events"
    view_id: str = "v_llm_response"
    traces_path: str | None = None
    bound_host: str | None = None
    configured_allowed_hosts: tuple[str, ...] = ()
    adk_proxy_port: int = 8082
    pricing: PricingEngine = PricingEngine(
        TokenomicsConfig(models_config_path=_PACKAGED_MODELS_CFG)
    )
    _pricing_config_sig: tuple[str, int, int] | None = None
    ACTIVE_BQ_CONFIG: dict[str, str] = {
        "project_id": "",
        "dataset_id": "bq_adk_ds",
        "table_id": "adk_agent_events",
        "view_id": "v_llm_response",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    @staticmethod
    def _stat_models_config_sig() -> tuple[str, int, int]:
        cfg_path = get_models_config_path()
        abs_p = os.path.abspath(cfg_path)
        try:
            st = os.stat(abs_p)
            return (abs_p, int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return (abs_p, -1, -1)

    def _get_active_pricing_engine(self) -> PricingEngine:
        with _MODELS_CONFIG_LOCK:
            cur_sig = TokenomicsWebHandler._stat_models_config_sig()
            if (
                TokenomicsWebHandler._pricing_config_sig is not None
                and cur_sig != TokenomicsWebHandler._pricing_config_sig
            ):
                rebuilt = TokenomicsWebHandler._build_sanitized_pricing_engine()
                TokenomicsWebHandler.pricing = rebuilt
                TokenomicsWebHandler._pricing_config_sig = cur_sig
                TokenomicsWebHandler._clear_bq_cache()
                self.pricing = rebuilt
                return rebuilt
        return (
            getattr(self, "pricing", None)
            or TokenomicsWebHandler.pricing
            or PricingEngine()
        )

    @classmethod
    def _clear_bq_cache(cls) -> None:
        with _BQ_CACHE_LOCK:
            _BQ_CACHE.clear()

    @classmethod
    def _get_bq_cached(cls, key: tuple[Any, ...]) -> Any | None:
        now = time.monotonic()
        with _BQ_CACHE_LOCK:
            entry = _BQ_CACHE.get(key)
            if entry is not None:
                ts, val = entry
                if (now - ts) < BQ_CACHE_TTL_SECONDS:
                    return copy.deepcopy(val)
                _BQ_CACHE.pop(key, None)
        return None

    @classmethod
    def _set_bq_cached(cls, key: tuple[Any, ...], val: Any) -> None:
        with _BQ_CACHE_LOCK:
            _BQ_CACHE[key] = (time.monotonic(), copy.deepcopy(val))

    @classmethod
    def set_server_context(
        cls,
        project_id: str,
        dataset_id: str = "bq_adk_ds",
        table_id: str = "adk_agent_events",
        view_id: str = "v_llm_response",
        traces_path: str | None = None,
        allowed_hosts: Sequence[str] | None = None,
        bound_host: str | None = None,
    ):
        clean_proj = ""
        if project_id is not None and str(project_id).strip() != "":
            clean_proj = validate_bq_project_id(str(project_id).strip())
        clean_ds = validate_bq_identifier(str(dataset_id), "dataset_id")
        clean_tbl = validate_bq_identifier(str(table_id), "table_id")
        clean_vw = validate_bq_identifier(str(view_id), "view_id")
        clean_allowed = validate_allowed_hosts(allowed_hosts)
        clean_bound_host = (
            str(bound_host).strip()
            if (bound_host is not None and str(bound_host).strip())
            else None
        )

        cls.project_id = clean_proj
        cls.dataset_id = clean_ds
        cls.table_id = clean_tbl
        cls.view_id = clean_vw
        cls.traces_path = traces_path
        cls.bound_host = clean_bound_host
        cls.configured_allowed_hosts = clean_allowed
        cls.pricing = cls._build_sanitized_pricing_engine()
        cls._pricing_config_sig = cls._stat_models_config_sig()
        cls.ACTIVE_BQ_CONFIG = {
            "project_id": clean_proj,
            "dataset_id": clean_ds,
            "table_id": clean_tbl,
            "view_id": clean_vw,
        }
        cls._clear_bq_cache()

    def get_bq_config(self) -> dict[str, Any]:
        cfg = getattr(self, "ACTIVE_BQ_CONFIG", None) or {
            "project_id": getattr(self, "project_id", ""),
            "dataset_id": getattr(self, "dataset_id", "bq_adk_ds"),
            "table_id": getattr(self, "table_id", "adk_agent_events"),
            "view_id": getattr(self, "view_id", "v_llm_response"),
        }
        return dict(cfg)

    def set_bq_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(
                "BigQuery configuration payload must be a JSON object."
            )

        staged: dict[str, str] = {}
        if "project_id" in payload and payload["project_id"] is not None:
            raw_proj = str(payload["project_id"])
            if raw_proj.strip() == "":
                if raw_proj != "":
                    raise ValueError(
                        "Invalid BigQuery project_id: whitespace-only string."
                    )
                staged["project_id"] = ""
            else:
                staged["project_id"] = validate_bq_project_id(raw_proj)

        for field_name in ("dataset_id", "table_id", "view_id"):
            if field_name in payload and payload[field_name] is not None:
                staged[field_name] = validate_bq_identifier(
                    str(payload[field_name]), field_name
                )

        base_cfg = dict(self.get_bq_config())
        base_cfg.update(staged)
        self.ACTIVE_BQ_CONFIG = dict(base_cfg)
        TokenomicsWebHandler.ACTIVE_BQ_CONFIG = dict(base_cfg)
        for k, v in staged.items():
            setattr(TokenomicsWebHandler, k, v)
            setattr(self, k, v)
        self._clear_bq_cache()
        return dict(self.ACTIVE_BQ_CONFIG)

    def _validated_bq_identifiers(self) -> tuple[str, str, str, str]:
        raw_proj = str(getattr(self, "project_id", "") or "").strip()
        if not raw_proj:
            raise RuntimeError(
                "No GCP project configured in .env or CLI (--project / GOOGLE_CLOUD_PROJECT)."
            )
        proj = validate_bq_project_id(raw_proj)
        ds = validate_bq_identifier(
            str(getattr(self, "dataset_id", "bq_adk_ds") or "bq_adk_ds"),
            "dataset_id",
        )
        tbl = validate_bq_identifier(
            str(
                getattr(self, "table_id", "adk_agent_events")
                or "adk_agent_events"
            ),
            "table_id",
        )
        vw = validate_bq_identifier(
            str(getattr(self, "view_id", "v_llm_response") or "v_llm_response"),
            "view_id",
        )
        return proj, ds, tbl, vw

    def _calc_model_cost(
        self,
        model_name: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        models_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        engine = (
            self._get_active_pricing_engine()
            if hasattr(self, "_get_active_pricing_engine")
            else (getattr(self, "pricing", None) or PricingEngine())
        )
        rate = engine.get_rate(model_name)
        in_rate = float(rate.get("input", 0.75))
        cache_rate = float(rate.get("cached", 0.075))
        out_rate = float(rate.get("output", 3.75))

        if models_config and isinstance(models_config, dict):
            norm_query = PricingEngine._normalize_model_id(model_name)
            for m in models_config.get("models", []):
                if not isinstance(m, dict):
                    continue
                m_id = str(m.get("id", ""))
                short_id = PricingEngine._normalize_model_id(m_id)
                if model_name in (
                    m_id,
                    short_id,
                    m.get("name"),
                ) or norm_query in (m_id, short_id):
                    p = m.get("pricing", {})
                    in_rate = float(
                        p.get("input_per_million", p.get("input", in_rate))
                    )
                    cache_rate = float(
                        p.get("cached_per_million", p.get("cached", cache_rate))
                    )
                    out_rate = float(
                        p.get("output_per_million", p.get("output", out_rate))
                    )
                    break

        in_tok = max(0, int(input_tokens or 0))
        cac_tok = max(0, int(cached_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        thk_tok = max(0, int(thinking_tokens or 0))

        input_cost = (in_tok / 1_000_000.0) * in_rate
        cached_cost = (cac_tok / 1_000_000.0) * cache_rate
        output_cost = ((out_tok + thk_tok) / 1_000_000.0) * out_rate
        total_cost = input_cost + cached_cost + output_cost
        uncached_cost = (
            (in_tok + cac_tok) / 1_000_000.0
        ) * in_rate + output_cost
        savings_dollars = max(0.0, uncached_cost - total_cost)
        savings_pct = (
            (savings_dollars / uncached_cost * 100.0)
            if uncached_cost > 0
            else 0.0
        )

        return {
            "input_rate": in_rate,
            "cached_rate": cache_rate,
            "output_rate": out_rate,
            "input_cost": input_cost,
            "cached_cost": cached_cost,
            "output_cost": output_cost,
            "total_cost": total_cost,
            "uncached_cost": uncached_cost,
            "savings_dollars": savings_dollars,
            "savings_pct": savings_pct,
        }

    def _empty_finops_summary(
        self,
        message: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "error" if message else "success",
            "message": message,
            "kpis": {
                "total_spend": 0.0,
                "total_tokens": 0,
                "total_input": 0,
                "total_cached": 0,
                "total_output": 0,
                "total_thinking": 0,
                "total_turns": 0,
                "blended_cost_per_1m": 0.0,
                "top_provider": "None",
                "top_provider_share": 0.0,
                "optimization_savings_dollars": 0.0,
                "optimization_savings_pct": 0.0,
            },
            "provider_breakdown": {
                "Google Gemini": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Anthropic Claude": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "OpenAI": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Open-Weight / MaaS": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Other": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
            },
            "matrix_rows": [],
            "timeline": [],
        }
        if message:
            out["error"] = message
        if extra:
            out.update(extra)
        return out

    @staticmethod
    def _row_val(row_obj: Any, attr_name: str, default: Any = None) -> Any:
        if row_obj is None:
            return default
        if isinstance(row_obj, Mapping):
            val = row_obj.get(attr_name, default)
            return default if val is None else val
        if hasattr(row_obj, "get") and callable(row_obj.get):
            try:
                val = row_obj.get(attr_name, default)
                return default if val is None else val
            except Exception:
                pass
        try:
            val = getattr(row_obj, attr_name, default)
            return default if val is None else val
        except Exception:
            return default

    @staticmethod
    def _first_not_none(*vals: Any) -> Any:
        for v in vals:
            if v is not None:
                return v
        return None

    @staticmethod
    def _extract_content_function_calls(
        content_dict: Mapping[str, Any],
    ) -> tuple[list[str], list[str]]:
        """Extracts tool names and activated skill names from content.parts[].function_call when attributes.tools_called is absent."""
        found_tools: list[str] = []
        found_skills: list[str] = []

        part_groups: list[Any] = []
        if isinstance(content_dict.get("parts"), list):
            part_groups.append(content_dict["parts"])
        resp_obj = content_dict.get("response")
        if isinstance(resp_obj, Mapping) and isinstance(
            resp_obj.get("parts"), list
        ):
            part_groups.append(resp_obj["parts"])
        candidates = content_dict.get("candidates")
        if isinstance(candidates, list):
            for cand in candidates:
                if isinstance(cand, Mapping):
                    c_content = cand.get("content")
                    if isinstance(c_content, Mapping) and isinstance(
                        c_content.get("parts"), list
                    ):
                        part_groups.append(c_content["parts"])

        for parts in part_groups:
            for part in parts:
                if not isinstance(part, Mapping):
                    continue
                fc = part.get("function_call") or part.get("functionCall")
                if not isinstance(fc, Mapping):
                    continue
                fc_name = str(fc.get("name") or "").strip()
                if not fc_name:
                    continue
                if fc_name not in found_tools:
                    found_tools.append(fc_name)
                raw_args = (
                    fc.get("args")
                    if fc.get("args") is not None
                    else fc.get("arguments")
                )
                fc_args: dict[str, Any] = {}
                if isinstance(raw_args, Mapping):
                    fc_args = dict(raw_args)
                elif isinstance(raw_args, str) and raw_args.strip().startswith(
                    "{"
                ):
                    try:
                        parsed_a = json.loads(raw_args)
                        if isinstance(parsed_a, Mapping):
                            fc_args = dict(parsed_a)
                    except Exception:
                        pass
                if (
                    fc_args
                    and "skill" in fc_name.lower()
                    and "unload" not in fc_name.lower()
                ):
                    raw_sk = (
                        fc_args.get("skill_name")
                        or fc_args.get("name")
                        or fc_args.get("skill")
                    )
                    if raw_sk:
                        sk_str = str(raw_sk).strip()
                        if (
                            sk_str
                            and sk_str.lower() != "unload"
                            and sk_str not in found_skills
                        ):
                            found_skills.append(sk_str)
        return found_tools, found_skills

    @classmethod
    def _parse_bq_event_row(
        cls, r: Any, pricing: PricingEngine | None = None
    ) -> dict[str, Any]:
        ts = cls._row_val(r, "timestamp", "")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()

        raw_content = cls._row_val(r, "content", None)
        content_dict: dict[str, Any] = {}
        if isinstance(raw_content, dict):
            content_dict = raw_content
        elif isinstance(raw_content, str) and raw_content.strip().startswith(
            "{"
        ):
            try:
                content_dict = json.loads(raw_content)
            except Exception:
                pass

        raw_attrs = cls._row_val(r, "attributes", None)
        attr_dict: dict[str, Any] = {}
        if isinstance(raw_attrs, dict):
            attr_dict = raw_attrs
        elif isinstance(raw_attrs, str) and raw_attrs.strip().startswith("{"):
            try:
                attr_dict = json.loads(raw_attrs)
            except Exception:
                pass

        raw_json_val = cls._row_val(r, "raw_json", None)
        raw_meta_dict: dict[str, Any] = {}
        if isinstance(raw_json_val, dict):
            raw_meta_dict = raw_json_val
        elif isinstance(raw_json_val, str) and raw_json_val.strip().startswith(
            "{"
        ):
            try:
                raw_meta_dict = json.loads(raw_json_val)
            except Exception:
                pass

        usage_meta_val = attr_dict.get("usage_metadata")
        usage_meta: dict[str, Any] = (
            usage_meta_val if isinstance(usage_meta_val, dict) else {}
        )
        content_usage_val = content_dict.get("usage")
        content_usage: dict[str, Any] = (
            content_usage_val if isinstance(content_usage_val, dict) else {}
        )
        sess_meta_val = attr_dict.get("session_metadata")
        sess_meta: dict[str, Any] = (
            sess_meta_val if isinstance(sess_meta_val, dict) else {}
        )

        raw_ev_type = cls._row_val(r, "event_type", None)
        ev_type = (
            str(raw_ev_type).strip()
            if (raw_ev_type is not None and str(raw_ev_type).strip())
            else "LLM_RESPONSE"
        )
        session_id = str(
            cls._row_val(r, "session_id", "")
            or sess_meta.get("session_id")
            or ""
        )
        app_name = str(
            cls._row_val(r, "app_name", None)
            or cls._row_val(r, "agent", None)
            or sess_meta.get("app_name")
            or "agent"
        )

        invocation_id = str(
            cls._row_val(r, "invocation_id", None)
            or attr_dict.get("invocation_id")
            or content_dict.get("invocation_id")
            or raw_meta_dict.get("invocation_id")
            or ""
        )

        user_query = cls._row_val(r, "user_query", None)
        if not user_query:
            user_query = (
                content_dict.get("user_query")
                or content_dict.get("prompt")
                or content_dict.get("text_summary")
                or attr_dict.get("user_query")
                or attr_dict.get("prompt")
                or sess_meta.get("user_query")
            )
        if (
            not user_query
            and isinstance(content_dict.get("parts"), list)
            and ev_type.upper() in ("USER_MESSAGE_RECEIVED", "LLM_REQUEST")
        ):
            parts_txt = [
                str(p.get("text", "")).strip()
                for p in content_dict["parts"]
                if isinstance(p, dict) and p.get("text")
            ]
            if parts_txt:
                user_query = " ".join(parts_txt).strip()
        if not user_query:
            req_contents = content_dict.get("contents") or (
                content_dict.get("request", {}).get("contents")
                if isinstance(content_dict.get("request"), dict)
                else None
            )
            if isinstance(req_contents, list):
                fallback_q = ""
                for c_item in reversed(req_contents):
                    if isinstance(c_item, dict):
                        c_role = c_item.get("role", "user")
                        if c_role in ("user", None):
                            c_parts = c_item.get("parts") or []
                            c_txt = (
                                " ".join(
                                    str(p.get("text", "")).strip()
                                    for p in c_parts
                                    if isinstance(p, dict) and p.get("text")
                                ).strip()
                                or str(c_item.get("text") or "").strip()
                            )
                            if c_txt:
                                if c_txt.startswith("For context:"):
                                    if not fallback_q:
                                        fallback_q = c_txt
                                    continue
                                user_query = c_txt
                                break
                if not user_query and fallback_q:
                    user_query = fallback_q
        if not user_query:
            user_query = (
                f"Tool: {content_dict.get('tool')}"
                if content_dict.get("tool")
                else ""
            ) or (
                raw_content
                if isinstance(raw_content, str)
                and not raw_content.strip().startswith("{")
                else ""
            )

        agent_response = cls._row_val(
            r, "agent_response", None
        ) or cls._row_val(r, "response", None)
        if not agent_response:
            agent_response = (
                content_dict.get("response")
                or content_dict.get("result")
                or content_dict.get("error_traceback")
                or ""
            )
        if (
            not agent_response
            and ev_type.upper() == "LLM_RESPONSE"
            and isinstance(content_dict.get("parts"), list)
        ):
            resp_parts_txt = [
                str(p.get("text", ""))
                for p in content_dict["parts"]
                if isinstance(p, dict)
                and p.get("text")
                and not p.get("thought")
            ]
            if resp_parts_txt:
                agent_response = "".join(resp_parts_txt)
        if isinstance(agent_response, (dict, list)):
            try:
                if (
                    isinstance(agent_response, dict)
                    and "parts" in agent_response
                    and isinstance(agent_response["parts"], list)
                ):
                    joined_txt = "".join(
                        str(p.get("text", ""))
                        for p in agent_response["parts"]
                        if isinstance(p, dict)
                        and p.get("text")
                        and not p.get("thought")
                    )
                    has_fc = any(
                        isinstance(p, dict)
                        and (p.get("function_call") or p.get("functionCall"))
                        for p in agent_response["parts"]
                    )
                    agent_response = (
                        joined_txt
                        if (joined_txt or has_fc)
                        else json.dumps(agent_response)
                    )
                else:
                    agent_response = json.dumps(agent_response)
            except Exception:
                agent_response = str(agent_response)

        raw_tool_use = cls._first_not_none(
            cls._row_val(r, "usage_tool_use_tokens", None),
            usage_meta.get("tool_use_prompt_token_count"),
            usage_meta.get("toolUsePromptTokenCount"),
            attr_dict.get("usage_tool_use_tokens"),
        )
        tool_use_tokens = (
            _safe_nonneg_int(raw_tool_use) if raw_tool_use is not None else 0
        )

        raw_cached = cls._first_not_none(
            cls._row_val(r, "cached_tokens", None),
            cls._row_val(r, "usage_cached_tokens", None),
            usage_meta.get("cached_content_token_count"),
            usage_meta.get("cachedContentTokenCount"),
            usage_meta.get("cacheReadTokens"),
            attr_dict.get("usage_cached_tokens"),
        )
        cached_tokens = (
            _safe_nonneg_int(raw_cached) if raw_cached is not None else 0
        )

        row_input_tok = cls._row_val(r, "input_tokens", None)
        row_usage_prompt = cls._row_val(r, "usage_prompt_tokens", None)
        row_prompt_tok = cls._row_val(r, "prompt_tokens", None)

        if (
            row_input_tok is not None
            and row_usage_prompt is None
            and row_prompt_tok is None
        ):
            fresh_input_tokens = _safe_nonneg_int(row_input_tok)
            prompt_tokens = fresh_input_tokens + cached_tokens
        else:
            if row_usage_prompt is not None:
                prompt_tokens = (
                    _safe_nonneg_int(row_usage_prompt) + tool_use_tokens
                )
            elif row_prompt_tok is not None:
                prompt_tokens = _safe_nonneg_int(row_prompt_tok)
            else:
                raw_um_prompt = cls._first_not_none(
                    usage_meta.get("prompt_token_count"),
                    usage_meta.get("promptTokens"),
                )
                if raw_um_prompt is not None:
                    prompt_tokens = (
                        _safe_nonneg_int(raw_um_prompt) + tool_use_tokens
                    )
                elif content_usage.get("prompt") is not None:
                    prompt_tokens = (
                        _safe_nonneg_int(content_usage.get("prompt"))
                        + tool_use_tokens
                    )
                elif attr_dict.get("usage_prompt_tokens") is not None:
                    prompt_tokens = (
                        _safe_nonneg_int(attr_dict.get("usage_prompt_tokens"))
                        + tool_use_tokens
                    )
                else:
                    prompt_tokens = tool_use_tokens
            if prompt_tokens > 0 and cached_tokens > prompt_tokens:
                cached_tokens = prompt_tokens
            fresh_input_tokens = max(0, prompt_tokens - cached_tokens)

        raw_out = cls._first_not_none(
            cls._row_val(r, "output_tokens", None),
            cls._row_val(r, "usage_completion_tokens", None),
            content_usage.get("completion"),
            usage_meta.get("candidates_token_count"),
            usage_meta.get("candidatesTokenCount"),
            attr_dict.get("usage_completion_tokens"),
        )
        output_tokens = _safe_nonneg_int(raw_out) if raw_out is not None else 0

        raw_thk = cls._first_not_none(
            cls._row_val(r, "thinking_tokens", None),
            cls._row_val(r, "usage_thinking_tokens", None),
            usage_meta.get("thoughts_token_count"),
            usage_meta.get("thoughtsTokenCount"),
            usage_meta.get("thinkingOutputTokens"),
            attr_dict.get("usage_thinking_tokens"),
        )
        thinking_tokens = (
            _safe_nonneg_int(raw_thk) if raw_thk is not None else 0
        )

        model_name = str(
            cls._row_val(r, "model_name", None)
            or cls._row_val(r, "model_version", None)
            or attr_dict.get("model_version")
            or attr_dict.get("model")
            or content_dict.get("model_version")
            or "Gemini 3.8 Flash"
        )

        raw_lat = cls._first_not_none(
            cls._row_val(r, "total_ms", None),
            cls._row_val(r, "latency_ms", None),
            raw_meta_dict.get("latency_ms"),
        )
        latency_ms = 0
        if isinstance(raw_lat, (int, float)) and not isinstance(raw_lat, bool):
            latency_ms = (
                _safe_nonneg_int(raw_lat)
                if math.isfinite(float(raw_lat))
                else 0
            )
        elif isinstance(raw_lat, dict):
            latency_ms = _safe_nonneg_int(raw_lat.get("total_ms") or 0)
        elif isinstance(raw_lat, str):
            s_lat = raw_lat.strip()
            if s_lat.startswith("{"):
                try:
                    latency_ms = _safe_nonneg_int(
                        json.loads(s_lat).get("total_ms") or 0
                    )
                except Exception:
                    latency_ms = 0
            elif s_lat:
                try:
                    f_lat = float(s_lat)
                    latency_ms = (
                        max(0, round(f_lat)) if math.isfinite(f_lat) else 0
                    )
                except (ValueError, TypeError):
                    latency_ms = 0

        est_cost_raw = cls._first_not_none(
            cls._row_val(r, "estimated_cost", None),
            cls._row_val(r, "total_cost", None),
            attr_dict.get("total_cost"),
        )
        savings_dollars = 0.0
        uncached_cost = 0.0
        if pricing is not None and (
            prompt_tokens > 0 or output_tokens > 0 or thinking_tokens > 0
        ):
            bd = pricing.calculate_turn_cost(
                model_name,
                fresh_input_tokens,
                cached_tokens,
                output_tokens,
                thinking_tokens,
            )
            estimated_cost = float(bd["total_cost"])
            savings_dollars = float(bd["savings_dollars"])
            uncached_cost = float(bd["hypothetical_uncached_cost"])
        elif est_cost_raw is not None:
            try:
                estimated_cost = max(0.0, float(est_cost_raw or 0.0))
            except (ValueError, TypeError):
                estimated_cost = 0.0
            uncached_cost = estimated_cost
        elif prompt_tokens > 0 or output_tokens > 0 or thinking_tokens > 0:
            pe = pricing or PricingEngine()
            bd = pe.calculate_turn_cost(
                model_name,
                fresh_input_tokens,
                cached_tokens,
                output_tokens,
                thinking_tokens,
            )
            estimated_cost = float(bd["total_cost"])
            savings_dollars = float(bd["savings_dollars"])
            uncached_cost = float(bd["hypothetical_uncached_cost"])
        else:
            estimated_cost = 0.0

        tools_list: list[str] = []
        raw_tools_src = cls._first_not_none(
            cls._row_val(r, "tools_called", None),
            attr_dict.get("tools_called"),
            cls._row_val(r, "invoked_tools", None),
            content_dict.get("tool"),
        )
        if isinstance(raw_tools_src, (list, tuple, set)):
            for item in raw_tools_src:
                s_item = str(item).strip()
                if (
                    s_item
                    and s_item != "None (Direct Text)"
                    and s_item not in tools_list
                ):
                    tools_list.append(s_item)
        elif isinstance(raw_tools_src, str) and raw_tools_src.strip():
            for s_item in _split_top_level_csv(raw_tools_src):
                if (
                    s_item
                    and s_item != "None (Direct Text)"
                    and s_item not in tools_list
                ):
                    tools_list.append(s_item)

        skills_list: list[str] = []
        raw_skills_src = cls._first_not_none(
            cls._row_val(r, "skills_active", None),
            attr_dict.get("skills_active"),
            cls._row_val(r, "invoked_skills", None),
        )
        if isinstance(raw_skills_src, (list, tuple, set)):
            for item in raw_skills_src:
                s_item = str(item).strip()
                if (
                    s_item
                    and s_item not in ("None", "unload")
                    and s_item not in skills_list
                ):
                    skills_list.append(s_item)
        elif isinstance(raw_skills_src, str) and raw_skills_src.strip():
            for s_item in _split_top_level_csv(raw_skills_src):
                if (
                    s_item
                    and s_item not in ("None", "unload")
                    and s_item not in skills_list
                ):
                    skills_list.append(s_item)

        if (
            not tools_list
            and ev_type.upper() == "LLM_RESPONSE"
            and content_dict
        ):
            fc_tools, fc_skills = cls._extract_content_function_calls(
                content_dict
            )
            for ft in fc_tools:
                if ft not in tools_list:
                    tools_list.append(ft)
            if not skills_list:
                for fs in fc_skills:
                    if fs not in skills_list:
                        skills_list.append(fs)

        inv_tools = ", ".join(tools_list)
        inv_skills = ", ".join(skills_list)

        sub_agent = str(
            cls._row_val(r, "sub_agent", None)
            or attr_dict.get("sub_agent")
            or raw_meta_dict.get("sub_agent")
            or app_name
        )
        orchestration_flow = str(
            cls._row_val(r, "orchestration_flow", None)
            or attr_dict.get("orchestration_flow")
            or raw_meta_dict.get("orchestration_flow")
            or ""
        )

        return {
            "timestamp": str(ts),
            "event_type": ev_type,
            "session_id": session_id,
            "invocation_id": invocation_id,
            "app_name": sub_agent,
            "sub_agent": sub_agent,
            "orchestration_flow": orchestration_flow,
            "user_query": str(user_query or ""),
            "agent_response": str(agent_response or ""),
            "prompt_tokens": prompt_tokens,
            "input_tokens": fresh_input_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "usage_tool_use_tokens": tool_use_tokens,
            "latency_ms": latency_ms,
            "estimated_cost": estimated_cost,
            "total_cost": estimated_cost,
            "savings_dollars": savings_dollars,
            "uncached_cost": uncached_cost,
            "source": str(
                cls._row_val(r, "source", "adk_bigquery") or "adk_bigquery"
            ),
            "model_name": model_name,
            "tools_called": list(tools_list),
            "skills_active": list(skills_list),
            "invoked_tools": inv_tools,
            "invoked_skills": inv_skills,
        }

    @classmethod
    def _correlate_bq_events(
        cls,
        parsed_rows: list[dict[str, Any]],
        filter_llm_turns: bool = False,
    ) -> list[dict[str, Any]]:
        """Correlates USER_MESSAGE_RECEIVED / LLM_REQUEST prompts and TOOL_* events onto LLM_RESPONSE rows."""
        if not parsed_rows:
            return []

        query_by_inv: dict[str, str] = {}
        query_by_sess: dict[str, str] = {}
        tools_by_inv: dict[str, list[str]] = {}
        skills_by_inv: dict[str, list[str]] = {}

        for ev in parsed_rows:
            sid = str(ev.get("session_id") or "")
            inv = str(ev.get("invocation_id") or "")
            ev_type = str(ev.get("event_type") or "LLM_RESPONSE").upper()
            uq = str(ev.get("user_query") or "").strip()
            if uq and not uq.startswith("Tool:") and uq != "Turn":
                if inv and (
                    inv not in query_by_inv or not uq.startswith("For context:")
                ):
                    query_by_inv[inv] = uq
                if sid and (
                    sid not in query_by_sess
                    or not uq.startswith("For context:")
                ):
                    query_by_sess[sid] = uq
            if (
                ev_type in ("TOOL_STARTING", "TOOL_COMPLETED")
                or ev.get("tools_called")
                or ev.get("invoked_tools")
            ):
                ev_tools = (
                    list(ev["tools_called"])
                    if isinstance(ev.get("tools_called"), list)
                    and ev["tools_called"]
                    else _split_top_level_csv(
                        str(ev.get("invoked_tools") or "")
                    )
                )
                if not ev_tools and uq.startswith("Tool:"):
                    t_fallback = uq.split("Tool:", 1)[1].strip()
                    if t_fallback:
                        ev_tools = [t_fallback]
                if ev_tools and inv:
                    bucket = tools_by_inv.setdefault(inv, [])
                    for p_clean in ev_tools:
                        if p_clean and p_clean not in bucket:
                            bucket.append(p_clean)
            if ev.get("skills_active") or ev.get("invoked_skills"):
                ev_skills = (
                    list(ev["skills_active"])
                    if isinstance(ev.get("skills_active"), list)
                    and ev["skills_active"]
                    else _split_top_level_csv(
                        str(ev.get("invoked_skills") or "")
                    )
                )
                if ev_skills and inv:
                    sbucket = skills_by_inv.setdefault(inv, [])
                    for s_clean in ev_skills:
                        if (
                            s_clean
                            and s_clean != "unload"
                            and s_clean not in sbucket
                        ):
                            sbucket.append(s_clean)

        correlated: list[dict[str, Any]] = []
        active_sess_prompt: dict[str, str] = {}
        for ev in parsed_rows:
            sid = str(ev.get("session_id") or "")
            inv = str(ev.get("invocation_id") or "")
            ev_type = str(ev.get("event_type") or "LLM_RESPONSE").upper()
            uq = str(ev.get("user_query") or "").strip()
            if uq and not uq.startswith("Tool:") and uq != "Turn":
                if (
                    not uq.startswith("For context:")
                    or sid not in active_sess_prompt
                ):
                    active_sess_prompt[sid] = uq

            if ev_type == "LLM_RESPONSE" or (
                int(ev.get("prompt_tokens") or 0) > 0
                or int(ev.get("output_tokens") or 0) > 0
            ):
                if not uq or uq.startswith("Tool:"):
                    resolved_q = (
                        (query_by_inv.get(inv) if inv else None)
                        or active_sess_prompt.get(sid)
                        or (query_by_sess.get(sid) if sid else None)
                        or ""
                    )
                    if resolved_q:
                        ev["user_query"] = resolved_q
                if inv and inv in tools_by_inv:
                    existing = (
                        list(ev["tools_called"])
                        if isinstance(ev.get("tools_called"), list)
                        and ev["tools_called"]
                        else _split_top_level_csv(
                            str(ev.get("invoked_tools") or "")
                        )
                    )
                    for t_name in tools_by_inv[inv]:
                        if t_name not in existing:
                            existing.append(t_name)
                    ev["tools_called"] = existing
                    ev["invoked_tools"] = ", ".join(existing)
                if inv and inv in skills_by_inv:
                    existing_sk = (
                        list(ev["skills_active"])
                        if isinstance(ev.get("skills_active"), list)
                        and ev["skills_active"]
                        else _split_top_level_csv(
                            str(ev.get("invoked_skills") or "")
                        )
                    )
                    for s_name in skills_by_inv[inv]:
                        if s_name not in existing_sk:
                            existing_sk.append(s_name)
                    ev["skills_active"] = existing_sk
                    ev["invoked_skills"] = ", ".join(existing_sk)
                if not ev.get("agent_response") and ev.get("invoked_tools"):
                    ev["agent_response"] = f"🛠️ Tool call: {ev['invoked_tools']}"
                correlated.append(ev)
            elif not filter_llm_turns:
                correlated.append(ev)

        return (
            correlated
            if (correlated or not filter_llm_turns)
            else list(parsed_rows)
        )

    @staticmethod
    def _is_missing_bq_table_error(exc: BaseException) -> bool:
        cls_name = type(exc).__name__
        cls_low = cls_name.lower()
        if cls_name == "NotFound" or cls_low in ("notfound", "stubnotfound"):
            return True
        if cls_name in (
            "Forbidden",
            "Unauthorized",
            "PermissionDenied",
            "Unauthenticated",
            "PermissionError",
        ) or cls_low in (
            "forbidden",
            "unauthorized",
            "permissiondenied",
            "unauthenticated",
            "permissionerror",
        ):
            return False

        raw_msg = str(exc or "")
        msg = raw_msg.lower()

        m_code = _LEADING_HTTP_CODE_RE.match(raw_msg)
        if m_code:
            http_code = m_code.group(1)
            if http_code in ("401", "403"):
                return False
            if http_code == "404" and not any(
                denied in msg
                for denied in ("access denied", "permission denied")
            ):
                return True

        if any(
            denied in msg
            for denied in (
                "access denied",
                "permission denied",
                "unauthenticated",
                "service_disabled",
                "accessnotconfigured",
                "bigquery api has not been used",
            )
        ):
            return False

        return any(
            marker in msg
            for marker in (
                "not found: table",
                "not found: dataset",
                "not found: view",
                "not found",
                "does not exist",
                "unrecognized name",
                "no such field",
            )
        )

    @staticmethod
    def _is_missing_tool_use_col_error(exc: BaseException) -> bool:
        msg = str(exc or "").lower()
        return "usage_tool_use_tokens" in msg and (
            "unrecognized name" in msg or "no such field" in msg
        )

    @staticmethod
    def _is_local_dev_mode(handler_obj: Any) -> bool:
        explicit_traces = getattr(handler_obj, "traces_path", None)
        has_explicit_traces = isinstance(explicit_traces, str) and bool(
            explicit_traces.strip()
        )
        has_project = bool(
            str(getattr(handler_obj, "project_id", "") or "").strip()
        )
        if has_explicit_traces and not has_project:
            return True
        if has_project:
            return False
        raw_sink = os.environ.get("TOKENOMICS_SINK")
        raw_mode = os.environ.get("TOKENOMICS_MODE")
        sink = raw_sink.strip().lower() if raw_sink else ""
        mode = raw_mode.strip().lower() if raw_mode else ""
        if sink == "memory" or mode in ("dev", "prototype") or not sink:
            return True
        return False

    @staticmethod
    def _format_friendly_bq_error(
        raw_err: str, proj: str, ds: str, tbl: str
    ) -> dict[str, str]:
        err_lower = str(raw_err or "").lower()
        proj_str = proj or "<your-gcp-project>"
        if (
            "bigquery api has not been used" in err_lower
            or "accessnotconfigured" in err_lower
            or "service_disabled" in err_lower
        ):
            return {
                "friendly_title": f"BigQuery API is not enabled in project '{proj_str}'",
                "friendly_summary": "BigQuery logging is optional during development. To stream production telemetry to BigQuery, enable the BigQuery API in your GCP project and provision the dataset.",
                "remediation_cmd": f"gcloud services enable bigquery.googleapis.com --project={proj_str} && python3 -c \"from adk_tokenomics_plugin import provision_bigquery_table; provision_bigquery_table('{proj_str}', '{ds}', '{tbl}')\"",
            }
        m_code = _LEADING_HTTP_CODE_RE.match(str(raw_err or ""))
        is_404_code = bool(m_code and m_code.group(1) == "404")
        if ("not found" in err_lower or is_404_code) and not any(
            denied in err_lower
            for denied in ("access denied", "permission denied")
        ):
            return {
                "friendly_title": f"BigQuery dataset or table '{proj_str}.{ds}.{tbl}' not provisioned yet",
                "friendly_summary": "The target BigQuery dataset/table does not exist yet in this project. Provision the schema with a one-line helper:",
                "remediation_cmd": f"python3 -c \"from adk_tokenomics_plugin import provision_bigquery_table; provision_bigquery_table('{proj_str}', '{ds}', '{tbl}')\"",
            }
        return {
            "friendly_title": f"BigQuery sink unreachable ({proj_str}.{ds})",
            "friendly_summary": "Could not query BigQuery. Verify your GCP credentials and project configuration, or run in local dev mode with --traces.",
            "remediation_cmd": f"gcloud auth application-default login && python3 -c \"from adk_tokenomics_plugin import provision_bigquery_table; provision_bigquery_table('{proj_str}', '{ds}', '{tbl}')\"",
        }

    def fetch_bq_stats(self) -> dict[str, Any]:
        raw_proj = str(getattr(self, "project_id", "") or "")
        raw_ds = str(getattr(self, "dataset_id", "bq_adk_ds") or "bq_adk_ds")
        raw_vw = str(
            getattr(self, "view_id", "v_llm_response") or "v_llm_response"
        )
        raw_tbl = str(
            getattr(self, "table_id", "adk_agent_events") or "adk_agent_events"
        )
        if TokenomicsWebHandler._is_local_dev_mode(self):
            env_mode = os.environ.get("TOKENOMICS_MODE")
            env_sink = os.environ.get("TOKENOMICS_SINK")
            return {
                "status": "dev_mode",
                "source": "local_traces",
                "mode": env_mode if env_mode is not None else "dev",
                "sink": env_sink if env_sink is not None else "memory",
                "traces_path": str(
                    getattr(self, "traces_path", None) or "artifacts/traces/"
                ),
                "target": f"{raw_proj}.{raw_ds}.{raw_vw}"
                if raw_proj
                else f"{raw_ds}.{raw_vw}",
                "total_turns": 0,
                "total_cost": 0.0,
                "total_input": 0,
                "total_fresh_input": 0,
                "total_cached": 0,
                "total_output": 0,
                "unique_sessions": 0,
            }
        try:
            from google.cloud import bigquery

            proj, ds, tbl, vw = self._validated_bq_identifiers()
            client = bigquery.Client(project=proj)

            def _stats_view_sql(prompt_expr: str) -> str:
                return f"""
                WITH base AS (
                    SELECT
                        session_id,
                        COALESCE(model_version, 'gemini-3.8-flash') AS model_version,
                        {prompt_expr} AS prompt_tokens,
                        COALESCE(usage_cached_tokens, 0) AS cached_tokens,
                        COALESCE(usage_completion_tokens, 0) AS output_tokens,
                        COALESCE(
                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64),
                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64),
                            0
                        ) AS thinking_tokens
                    FROM `{proj}.{ds}.{vw}`
                    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
                )
                SELECT
                    model_version,
                    COUNT(*) AS total_turns,
                    COALESCE(SUM(prompt_tokens), 0) AS total_input,
                    COALESCE(SUM(cached_tokens), 0) AS total_cached,
                    COALESCE(SUM(output_tokens), 0) AS total_output,
                    COALESCE(SUM(thinking_tokens), 0) AS total_thinking,
                    COUNT(DISTINCT session_id) AS unique_sessions,
                    (SELECT COUNT(DISTINCT session_id) FROM base) AS fleet_unique_sessions
                FROM base
                GROUP BY model_version
            """

            query = _stats_view_sql(
                "COALESCE(usage_prompt_tokens, 0) + COALESCE(usage_tool_use_tokens, 0)"
            )
            try:
                rows = list(client.query(query).result())
            except Exception as view_err:
                rows = None
                if self._is_missing_tool_use_col_error(view_err):
                    legacy_view_q = _stats_view_sql(
                        "COALESCE(usage_prompt_tokens, 0) + COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64), 0)"
                    )
                    try:
                        rows = list(client.query(legacy_view_q).result())
                    except Exception as legacy_err:
                        view_err = legacy_err
                if rows is None:
                    if not self._is_missing_bq_table_error(view_err):
                        raise view_err
                    last_tbl_err: BaseException = view_err
                    for candidate_tbl in dict.fromkeys(
                        [tbl, "adk_agent_events", "agent_events"]
                    ):
                        if not candidate_tbl:
                            continue
                        safe_tbl = validate_bq_identifier(
                            candidate_tbl, "table_id"
                        )
                        fallback_q = f"""
                            WITH base AS (
                                SELECT
                                    session_id,
                                    COALESCE(
                                        JSON_VALUE(attributes, '$.model_version'),
                                        JSON_VALUE(attributes, '$.model'),
                                        JSON_VALUE(content, '$.model_version'),
                                        'gemini-3.8-flash'
                                    ) AS model_version,
                                    (
                                        COALESCE(
                                            SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64),
                                            0
                                        )
                                        + COALESCE(
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64),
                                            0
                                        )
                                    ) AS prompt_tokens,
                                    COALESCE(
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64),
                                        0
                                    ) AS cached_tokens,
                                    COALESCE(
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64),
                                        SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64),
                                        0
                                    ) AS output_tokens,
                                    COALESCE(
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64),
                                        SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64),
                                        0
                                    ) AS thinking_tokens
                                FROM `{proj}.{ds}.{safe_tbl}`
                                WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
                                  AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
                            )
                            SELECT
                                model_version,
                                COUNT(*) AS total_turns,
                                COALESCE(SUM(prompt_tokens), 0) AS total_input,
                                COALESCE(SUM(cached_tokens), 0) AS total_cached,
                                COALESCE(SUM(output_tokens), 0) AS total_output,
                                COALESCE(SUM(thinking_tokens), 0) AS total_thinking,
                                COUNT(DISTINCT session_id) AS unique_sessions,
                                (SELECT COUNT(DISTINCT session_id) FROM base) AS fleet_unique_sessions
                            FROM base
                            GROUP BY model_version
                        """
                        try:
                            rows = list(client.query(fallback_q).result())
                            break
                        except Exception as tbl_err:
                            last_tbl_err = tbl_err
                            if not self._is_missing_bq_table_error(tbl_err):
                                raise tbl_err
                            continue
                    if rows is None:
                        raise last_tbl_err from None

            if rows:
                pe = (
                    self._get_active_pricing_engine()
                    if hasattr(self, "_get_active_pricing_engine")
                    else (getattr(self, "pricing", None) or PricingEngine())
                )
                models_cfg = (
                    self.load_models_config()
                    if hasattr(self, "load_models_config")
                    else None
                )
                tot_turns = 0
                tot_in = 0
                tot_fresh = 0
                tot_cac = 0
                tot_out = 0
                tot_thk = 0
                computed_cost = 0.0
                sum_unique_sessions = 0
                fleet_unique: int | None = None

                for r in rows:
                    r_turns = _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "total_turns", 0) or 0
                    )
                    r_in = _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "total_input", 0) or 0
                    )
                    r_cac = _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "total_cached", 0) or 0
                    )
                    r_out = _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "total_output", 0) or 0
                    )
                    r_thk = _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "total_thinking", 0)
                        or 0
                    )
                    r_fresh = max(0, r_in - r_cac)
                    m_ver = str(
                        TokenomicsWebHandler._row_val(r, "model_version", None)
                        or pe.config.default_model
                    )
                    row_cost_info = self._calc_model_cost(
                        m_ver,
                        r_fresh,
                        r_cac,
                        r_out,
                        r_thk,
                        models_config=models_cfg,
                    )
                    computed_cost += float(row_cost_info["total_cost"])
                    tot_turns += r_turns
                    tot_in += r_in
                    tot_fresh += r_fresh
                    tot_cac += r_cac
                    tot_out += r_out
                    tot_thk += r_thk
                    sum_unique_sessions += _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(r, "unique_sessions", 0)
                        or 0
                    )
                    raw_fleet_u = TokenomicsWebHandler._row_val(
                        r, "fleet_unique_sessions", None
                    )
                    if raw_fleet_u is not None and fleet_unique is None:
                        fleet_unique = _safe_nonneg_int(raw_fleet_u or 0)

                unique_sessions = (
                    fleet_unique
                    if fleet_unique is not None
                    else sum_unique_sessions
                )
                return {
                    "status": "success",
                    "source": "bigquery",
                    "total_turns": tot_turns,
                    "total_cost": round(computed_cost, 6),
                    "total_input": tot_in,
                    "total_fresh_input": tot_fresh,
                    "total_cached": tot_cac,
                    "total_output": tot_out,
                    "total_thinking": tot_thk,
                    "unique_sessions": unique_sessions,
                }
        except Exception as e:
            friendly = TokenomicsWebHandler._format_friendly_bq_error(
                str(e), raw_proj, raw_ds, raw_tbl
            )
            return {
                "status": "error",
                "source": "bigquery",
                "target": f"{raw_proj}.{raw_ds}.{raw_vw}"
                if raw_proj
                else f"{raw_ds}.{raw_vw}",
                "message": str(e),
                **friendly,
                "total_turns": 0,
                "total_cost": 0.0,
                "total_input": 0,
                "total_fresh_input": 0,
                "total_cached": 0,
                "total_output": 0,
                "unique_sessions": 0,
            }
        return {
            "status": "success",
            "source": "bigquery",
            "total_turns": 0,
            "total_cost": 0.0,
            "total_input": 0,
            "total_fresh_input": 0,
            "total_cached": 0,
            "total_output": 0,
            "unique_sessions": 0,
        }

    def fetch_bq_explorer_logs(
        self,
        limit: int = 50,
        offset: int = 0,
        app_name: str = "all",
        search: str = "",
    ) -> dict[str, Any]:
        raw_proj = str(getattr(self, "project_id", "") or "")
        raw_ds = str(getattr(self, "dataset_id", "bq_adk_ds") or "bq_adk_ds")
        raw_vw = str(
            getattr(self, "view_id", "v_llm_response") or "v_llm_response"
        )
        raw_tbl = str(
            getattr(self, "table_id", "adk_agent_events") or "adk_agent_events"
        )
        if TokenomicsWebHandler._is_local_dev_mode(self):
            env_mode = os.environ.get("TOKENOMICS_MODE")
            env_sink = os.environ.get("TOKENOMICS_SINK")
            return {
                "status": "dev_mode",
                "source": "local_traces",
                "mode": env_mode if env_mode is not None else "dev",
                "sink": env_sink if env_sink is not None else "memory",
                "traces_path": str(
                    getattr(self, "traces_path", None) or "artifacts/traces/"
                ),
                "project_id": raw_proj,
                "dataset_id": raw_ds,
                "table_id": raw_tbl,
                "target": f"{raw_proj}.{raw_ds}.{raw_vw}"
                if raw_proj
                else f"{raw_ds}.{raw_vw}",
                "total_rows": 0,
                "rows": [],
            }
        try:
            from google.cloud import bigquery

            proj, ds, tbl, vw = self._validated_bq_identifiers()
            safe_limit = min(500, max(1, int(limit)))
            safe_offset = max(0, int(offset))
            clean_app = str(app_name or "all").strip() or "all"
            clean_q = str(search or "").strip()

            client = bigquery.Client(project=proj)
            query_params = [
                bigquery.ScalarQueryParameter("app", "STRING", clean_app),
                bigquery.ScalarQueryParameter("q", "STRING", clean_q),
            ]
            job_config = bigquery.QueryJobConfig(query_parameters=query_params)

            view_filter = """
                WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
                  AND (@app = 'all' OR agent = @app OR COALESCE(JSON_VALUE(attributes, '$.sub_agent'), '') = @app)
                  AND (
                      @q = ''
                      OR CONTAINS_SUBSTR(COALESCE(session_id, ''), @q)
                      OR CONTAINS_SUBSTR(content, @q)
                      OR CONTAINS_SUBSTR(attributes, @q)
                  )
            """

            active_target = f"{proj}.{ds}.{vw}"
            try:
                count_q = f"SELECT COUNT(*) AS total FROM `{proj}.{ds}.{vw}` {view_filter}"
                count_rows = list(
                    client.query(count_q, job_config=job_config).result()
                )
                total_rows = (
                    _safe_nonneg_int(
                        TokenomicsWebHandler._row_val(count_rows[0], "total", 0)
                        or 0
                    )
                    if count_rows
                    else 0
                )

                data_q = f"""
                    SELECT
                        timestamp,
                        event_type,
                        agent,
                        session_id,
                        invocation_id,
                        model_version,
                        usage_prompt_tokens,
                        usage_tool_use_tokens,
                        usage_cached_tokens,
                        usage_completion_tokens,
                        total_ms,
                        user_query,
                        agent_response,
                        attributes,
                        content
                    FROM `{proj}.{ds}.{vw}`
                    {view_filter}
                    ORDER BY timestamp DESC
                    LIMIT {safe_limit} OFFSET {safe_offset}
                """
                try:
                    data_rows = list(
                        client.query(data_q, job_config=job_config).result()
                    )
                except Exception as data_view_err:
                    if self._is_missing_tool_use_col_error(data_view_err):
                        legacy_data_q = f"""
                            SELECT
                                timestamp,
                                event_type,
                                agent,
                                session_id,
                                invocation_id,
                                model_version,
                                usage_prompt_tokens,
                                usage_cached_tokens,
                                usage_completion_tokens,
                                total_ms,
                                user_query,
                                agent_response,
                                attributes,
                                content
                            FROM `{proj}.{ds}.{vw}`
                            {view_filter}
                            ORDER BY timestamp DESC
                            LIMIT {safe_limit} OFFSET {safe_offset}
                        """
                        data_rows = list(
                            client.query(
                                legacy_data_q, job_config=job_config
                            ).result()
                        )
                    else:
                        raise data_view_err
            except Exception as view_err:
                if not self._is_missing_bq_table_error(view_err):
                    raise view_err
                data_rows = None
                total_rows = 0
                base_filter = """
                    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
                      AND (@app = 'all' OR agent = @app OR COALESCE(JSON_VALUE(attributes, '$.sub_agent'), '') = @app)
                      AND (
                          @q = ''
                          OR CONTAINS_SUBSTR(COALESCE(session_id, ''), @q)
                          OR CONTAINS_SUBSTR(content, @q)
                          OR CONTAINS_SUBSTR(attributes, @q)
                      )
                """
                last_tbl_err: BaseException = view_err
                for candidate_tbl in dict.fromkeys(
                    [tbl, "adk_agent_events", "agent_events"]
                ):
                    if not candidate_tbl:
                        continue
                    safe_tbl = validate_bq_identifier(candidate_tbl, "table_id")
                    try:
                        active_target = f"{proj}.{ds}.{safe_tbl}"
                        c_q = f"SELECT COUNT(*) AS total FROM `{active_target}` {base_filter}"
                        c_rows = list(
                            client.query(c_q, job_config=job_config).result()
                        )
                        total_rows = (
                            _safe_nonneg_int(
                                TokenomicsWebHandler._row_val(
                                    c_rows[0], "total", 0
                                )
                                or 0
                            )
                            if c_rows
                            else 0
                        )

                        d_q = f"""
                            SELECT
                                timestamp,
                                event_type,
                                agent,
                                session_id,
                                invocation_id,
                                content,
                                attributes,
                                latency_ms
                            FROM `{active_target}`
                            {base_filter}
                            ORDER BY timestamp DESC
                            LIMIT {safe_limit} OFFSET {safe_offset}
                        """
                        data_rows = list(
                            client.query(d_q, job_config=job_config).result()
                        )
                        break
                    except Exception as tbl_err:
                        last_tbl_err = tbl_err
                        if not self._is_missing_bq_table_error(tbl_err):
                            raise tbl_err
                        continue
                if data_rows is None:
                    raise last_tbl_err from None

            pricing = (
                self._get_active_pricing_engine()
                if hasattr(self, "_get_active_pricing_engine")
                else (getattr(self, "pricing", None) or PricingEngine())
            )
            parsed_rows = [
                TokenomicsWebHandler._parse_bq_event_row(r, pricing=pricing)
                for r in (data_rows or [])
            ]

            return {
                "status": "success",
                "source": "bigquery",
                "target": active_target,
                "total_rows": total_rows,
                "rows": parsed_rows,
            }
        except Exception as e:
            friendly = TokenomicsWebHandler._format_friendly_bq_error(
                str(e), raw_proj, raw_ds, raw_tbl
            )
            return {
                "status": "error",
                "source": "bigquery",
                "target": f"{raw_proj}.{raw_ds}.{raw_vw}"
                if raw_proj
                else f"{raw_ds}.{raw_vw}",
                "message": str(e),
                **friendly,
                "total_rows": 0,
                "rows": [],
            }

    def fetch_finops_summary(
        self, timeframe: str = "all", provider: str = "all"
    ) -> dict[str, Any]:
        raw_proj = str(getattr(self, "project_id", "") or "")
        raw_ds = str(getattr(self, "dataset_id", "bq_adk_ds") or "bq_adk_ds")
        raw_vw = str(
            getattr(self, "view_id", "v_llm_response") or "v_llm_response"
        )
        raw_tbl = str(
            getattr(self, "table_id", "adk_agent_events") or "adk_agent_events"
        )
        try:
            from google.cloud import bigquery

            from adk_tokenomics_plugin.finops.aggregator import FinOpsAggregator

            proj, ds, tbl, vw = self._validated_bq_identifiers()
            client = bigquery.Client(project=proj)

            tf_clean = str(timeframe or "all").strip().lower()
            days_map = {
                "today": 1,
                "1d": 1,
                "7d": 7,
                "week": 7,
                "30d": 30,
                "month": 30,
                "all": 30,
            }
            window_days = days_map.get(tf_clean, 30)

            view_thinking_expr = (
                "COALESCE("
                "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), "
                "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64), "
                "0)"
            )

            def _finops_view_sqls(view_prompt_expr: str) -> tuple[str, str]:
                gq = f"""
                    SELECT
                        COALESCE(model_version, 'Gemini 3.8 Flash') AS model_name,
                        COALESCE(agent, 'agent') AS app_name,
                        COUNT(*) AS turns,
                        COALESCE(SUM(GREATEST(0, {view_prompt_expr} - COALESCE(usage_cached_tokens, 0))), 0) AS input_tokens,
                        COALESCE(SUM(COALESCE(usage_cached_tokens, 0)), 0) AS cached_tokens,
                        COALESCE(SUM(COALESCE(usage_completion_tokens, 0)), 0) AS output_tokens,
                        COALESCE(SUM({view_thinking_expr}), 0) AS thinking_tokens
                    FROM `{proj}.{ds}.{vw}`
                    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_days} DAY)
                    GROUP BY model_name, app_name
                """
                tq = f"""
                    SELECT
                        FORMAT_TIMESTAMP('%Y-%m-%d', timestamp) AS date_label,
                        COALESCE(model_version, 'Gemini 3.8 Flash') AS model_name,
                        COUNT(*) AS turns,
                        COALESCE(SUM(GREATEST(0, {view_prompt_expr} - COALESCE(usage_cached_tokens, 0))), 0) AS input_tokens,
                        COALESCE(SUM(COALESCE(usage_cached_tokens, 0)), 0) AS cached_tokens,
                        COALESCE(SUM(COALESCE(usage_completion_tokens, 0)), 0) AS output_tokens,
                        COALESCE(SUM({view_thinking_expr}), 0) AS thinking_tokens,
                        COALESCE(SUM({view_prompt_expr} + COALESCE(usage_completion_tokens, 0) + {view_thinking_expr}), 0) AS daily_tokens
                    FROM `{proj}.{ds}.{vw}`
                    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_days} DAY)
                    GROUP BY date_label, model_name
                    ORDER BY date_label ASC
                """
                return gq, tq

            group_q, timeline_q = _finops_view_sqls(
                "(COALESCE(usage_prompt_tokens, 0) + COALESCE(usage_tool_use_tokens, 0))"
            )
            try:
                group_rows = list(client.query(group_q).result())
                timeline_rows = list(client.query(timeline_q).result())
            except Exception as view_err:
                group_rows = None
                timeline_rows = None
                if self._is_missing_tool_use_col_error(view_err):
                    legacy_gq, legacy_tq = _finops_view_sqls(
                        "(COALESCE(usage_prompt_tokens, 0) + COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64), 0))"
                    )
                    try:
                        group_rows = list(client.query(legacy_gq).result())
                        timeline_rows = list(client.query(legacy_tq).result())
                    except Exception as legacy_err:
                        view_err = legacy_err
                if group_rows is None or timeline_rows is None:
                    if not self._is_missing_bq_table_error(view_err):
                        raise view_err
                    base_prompt_expr = (
                        "("
                        "COALESCE("
                        "SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64), "
                        "0) + COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64), "
                        "0)"
                        ")"
                    )
                    base_cached_expr = (
                        "COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64), "
                        "0)"
                    )
                    base_out_expr = (
                        "COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64), "
                        "0)"
                    )
                    base_think_expr = (
                        "COALESCE("
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64), "
                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64), "
                        "0)"
                    )
                    base_model_expr = (
                        "COALESCE("
                        "JSON_VALUE(attributes, '$.model_version'), "
                        "JSON_VALUE(attributes, '$.model'), "
                        "JSON_VALUE(content, '$.model_version'), "
                        "'Gemini 3.8 Flash')"
                    )
                    last_tbl_err: BaseException = view_err
                    for candidate_tbl in dict.fromkeys(
                        [tbl, "adk_agent_events", "agent_events"]
                    ):
                        if not candidate_tbl:
                            continue
                        safe_tbl = validate_bq_identifier(
                            candidate_tbl, "table_id"
                        )
                        fb_group_q = f"""
                            SELECT
                                {base_model_expr} AS model_name,
                                COALESCE(agent, 'agent') AS app_name,
                                COUNT(*) AS turns,
                                COALESCE(SUM(GREATEST(0, {base_prompt_expr} - {base_cached_expr})), 0) AS input_tokens,
                                COALESCE(SUM({base_cached_expr}), 0) AS cached_tokens,
                                COALESCE(SUM({base_out_expr}), 0) AS output_tokens,
                                COALESCE(SUM({base_think_expr}), 0) AS thinking_tokens
                            FROM `{proj}.{ds}.{safe_tbl}`
                            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_days} DAY)
                              AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
                            GROUP BY model_name, app_name
                        """
                        fb_timeline_q = f"""
                            SELECT
                                FORMAT_TIMESTAMP('%Y-%m-%d', timestamp) AS date_label,
                                {base_model_expr} AS model_name,
                                COUNT(*) AS turns,
                                COALESCE(SUM(GREATEST(0, {base_prompt_expr} - {base_cached_expr})), 0) AS input_tokens,
                                COALESCE(SUM({base_cached_expr}), 0) AS cached_tokens,
                                COALESCE(SUM({base_out_expr}), 0) AS output_tokens,
                                COALESCE(SUM({base_think_expr}), 0) AS thinking_tokens,
                                COALESCE(SUM({base_prompt_expr} + {base_out_expr} + {base_think_expr}), 0) AS daily_tokens
                            FROM `{proj}.{ds}.{safe_tbl}`
                            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_days} DAY)
                              AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
                            GROUP BY date_label, model_name
                            ORDER BY date_label ASC
                        """
                        try:
                            group_rows = list(client.query(fb_group_q).result())
                            timeline_rows = list(
                                client.query(fb_timeline_q).result()
                            )
                            break
                        except Exception as tbl_err:
                            last_tbl_err = tbl_err
                            if not self._is_missing_bq_table_error(tbl_err):
                                raise tbl_err
                            continue
                    if group_rows is None or timeline_rows is None:
                        raise last_tbl_err from None

            if not group_rows:
                empty = self._empty_finops_summary()
                empty["status"] = "success"
                return empty

            total_spend = 0.0
            total_input = 0
            total_cached = 0
            total_output = 0
            total_thinking = 0
            total_turns = 0
            savings_dollars = 0.0

            models_cfg = (
                self.load_models_config()
                if hasattr(self, "load_models_config")
                else None
            )
            prov_filter = str(provider or "all").strip().lower()
            prov_alias_map = {
                "google": "Google Gemini",
                "gemini": "Google Gemini",
                "google gemini": "Google Gemini",
                "anthropic": "Anthropic Claude",
                "claude": "Anthropic Claude",
                "anthropic claude": "Anthropic Claude",
                "openai": "OpenAI",
                "open_weight": "Open-Weight / MaaS",
                "open-weight": "Open-Weight / MaaS",
                "open-weight / maas": "Open-Weight / MaaS",
            }
            target_prov_label = prov_alias_map.get(prov_filter)

            provider_map = {
                "Google Gemini": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Anthropic Claude": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "OpenAI": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Open-Weight / MaaS": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
                "Other": {
                    "spend": 0.0,
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                },
            }
            matrix_rows: list[dict[str, Any]] = []
            for r in group_rows:
                m_name = str(
                    TokenomicsWebHandler._row_val(
                        r, "model_name", "Gemini 3.8 Flash"
                    )
                )
                prov = FinOpsAggregator.classify_provider(m_name)
                if (
                    prov_filter not in ("", "all")
                    and target_prov_label
                    and prov != target_prov_label
                ):
                    continue
                a_name = str(
                    TokenomicsWebHandler._row_val(r, "app_name", "agent")
                )
                turns = int(TokenomicsWebHandler._row_val(r, "turns", 0) or 0)
                in_tok = int(
                    TokenomicsWebHandler._row_val(r, "input_tokens", 0) or 0
                )
                cac_tok = int(
                    TokenomicsWebHandler._row_val(r, "cached_tokens", 0) or 0
                )
                out_tok = int(
                    TokenomicsWebHandler._row_val(r, "output_tokens", 0) or 0
                )
                thk_tok = int(
                    TokenomicsWebHandler._row_val(r, "thinking_tokens", 0) or 0
                )

                cost_info = self._calc_model_cost(
                    m_name,
                    in_tok,
                    cac_tok,
                    out_tok,
                    thk_tok,
                    models_config=models_cfg,
                )
                cost = float(cost_info["total_cost"])
                row_savings = float(cost_info["savings_dollars"])
                savings_dollars += row_savings
                tok_sum = in_tok + cac_tok + out_tok + thk_tok

                p_entry = provider_map.setdefault(
                    prov,
                    {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                )
                p_entry["spend"] += cost
                p_entry["turns"] += turns
                p_entry["input"] += in_tok
                p_entry["cached"] += cac_tok
                p_entry["output"] += out_tok
                p_entry["thinking"] += thk_tok

                total_spend += cost
                total_input += in_tok
                total_cached += cac_tok
                total_output += out_tok
                total_thinking += thk_tok
                total_turns += turns

                matrix_rows.append(
                    {
                        "provider": prov,
                        "model_name": m_name,
                        "app_name": a_name,
                        "turns": turns,
                        "input_tokens": in_tok,
                        "cached_tokens": cac_tok,
                        "output_tokens": out_tok,
                        "thinking_tokens": thk_tok,
                        "total_tokens": tok_sum,
                        "total_cost": round(cost, 5),
                        "spend_share_pct": 0.0,
                    }
                )

            matrix_rows.sort(key=lambda x: x["total_cost"], reverse=True)
            for mr in matrix_rows:
                mr["spend_share_pct"] = round(
                    (mr["total_cost"] / total_spend * 100.0)
                    if total_spend > 0
                    else 0.0,
                    1,
                )

            top_provider = "None"
            top_spend = 0.0
            for p_name, p_data in provider_map.items():
                if p_data["spend"] > top_spend:
                    top_spend = p_data["spend"]
                    top_provider = p_name

            total_tokens = (
                total_input + total_cached + total_output + total_thinking
            )
            hypothetical_base = total_spend + savings_dollars
            savings_pct = (
                (savings_dollars / hypothetical_base * 100.0)
                if hypothetical_base > 0
                else 0.0
            )
            blended_cost = (
                (total_spend / total_tokens * 1_000_000.0)
                if total_tokens > 0
                else 0.0
            )
            top_share = (
                (top_spend / total_spend * 100.0) if total_spend > 0 else 0.0
            )

            daily_buckets: dict[str, dict[str, Any]] = {}
            for tr in timeline_rows or []:
                tm_name = str(
                    TokenomicsWebHandler._row_val(
                        tr, "model_name", "Gemini 3.8 Flash"
                    )
                )
                if (
                    prov_filter not in ("", "all")
                    and target_prov_label
                    and FinOpsAggregator.classify_provider(tm_name)
                    != target_prov_label
                ):
                    continue
                d_lbl = str(TokenomicsWebHandler._row_val(tr, "date_label", ""))
                d_turns = int(
                    TokenomicsWebHandler._row_val(tr, "turns", 0) or 0
                )
                t_in = int(
                    TokenomicsWebHandler._row_val(tr, "input_tokens", 0) or 0
                )
                t_cac = int(
                    TokenomicsWebHandler._row_val(tr, "cached_tokens", 0) or 0
                )
                t_out = int(
                    TokenomicsWebHandler._row_val(tr, "output_tokens", 0) or 0
                )
                t_thk = int(
                    TokenomicsWebHandler._row_val(tr, "thinking_tokens", 0) or 0
                )
                raw_d_tok = TokenomicsWebHandler._row_val(
                    tr, "daily_tokens", None
                )
                d_tokens = (
                    int(raw_d_tok)
                    if raw_d_tok is not None
                    else (t_in + t_cac + t_out + t_thk)
                )
                d_cost = float(
                    self._calc_model_cost(
                        tm_name,
                        t_in,
                        t_cac,
                        t_out,
                        t_thk,
                        models_config=models_cfg,
                    )["total_cost"]
                )
                b = daily_buckets.setdefault(
                    d_lbl, {"turns": 0, "daily_tokens": 0, "daily_cost": 0.0}
                )
                b["turns"] += d_turns
                b["daily_tokens"] += d_tokens
                b["daily_cost"] += d_cost

            timeline = []
            cum_cost = 0.0
            for d_lbl in sorted(daily_buckets.keys()):
                b = daily_buckets[d_lbl]
                cum_cost += b["daily_cost"]
                timeline.append(
                    {
                        "date": d_lbl,
                        "turns": b["turns"],
                        "daily_tokens": b["daily_tokens"],
                        "daily_cost": round(b["daily_cost"], 5),
                        "cumulative_cost": round(cum_cost, 5),
                    }
                )

            return {
                "status": "success",
                "kpis": {
                    "total_spend": round(total_spend, 5),
                    "total_tokens": total_tokens,
                    "total_input": total_input,
                    "total_cached": total_cached,
                    "total_output": total_output,
                    "total_thinking": total_thinking,
                    "total_turns": total_turns,
                    "blended_cost_per_1m": round(blended_cost, 3),
                    "top_provider": top_provider,
                    "top_provider_share": round(top_share, 1),
                    "optimization_savings_dollars": round(savings_dollars, 4),
                    "optimization_savings_pct": round(savings_pct, 1),
                },
                "provider_breakdown": provider_map,
                "matrix_rows": matrix_rows,
                "timeline": timeline,
            }
        except Exception as e:
            if TokenomicsWebHandler._is_local_dev_mode(self):
                local_turns = (
                    self._load_local_trace_turns()
                    if hasattr(self, "_load_local_trace_turns")
                    else []
                )
            else:
                local_turns = []
            if isinstance(local_turns, list) and local_turns:
                from adk_tokenomics_plugin.finops.aggregator import (
                    FinOpsAggregator,
                )

                provider_map = {
                    "Google Gemini": {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                    "Anthropic Claude": {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                    "OpenAI": {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                    "Open-Weight / MaaS": {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                    "Other": {
                        "spend": 0.0,
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                    },
                }
                grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
                daily: dict[str, dict[str, Any]] = {}
                total_spend = 0.0
                total_input = 0
                total_cached = 0
                total_output = 0
                total_thinking = 0
                models_cfg = (
                    self.load_models_config()
                    if hasattr(self, "load_models_config")
                    else None
                )
                savings_dollars = 0.0
                for t in local_turns:
                    m_name = str(t.get("model_name", "gemini-3.8-flash"))
                    a_name = str(t.get("app_name", "local_agent"))
                    in_tok = int(t.get("input_tokens", 0))
                    cac_tok = int(t.get("cached_tokens", 0))
                    out_tok = int(t.get("output_tokens", 0))
                    thk_tok = int(t.get("thinking_tokens", 0))
                    cost = float(t.get("total_cost", 0.0))
                    savings_dollars += float(
                        TokenomicsWebHandler._calc_model_cost(
                            self,
                            m_name,
                            in_tok,
                            cac_tok,
                            out_tok,
                            thk_tok,
                            models_config=models_cfg,
                        )["savings_dollars"]
                    )
                    prov = FinOpsAggregator.classify_provider(m_name)
                    p_entry = provider_map.setdefault(
                        prov,
                        {
                            "spend": 0.0,
                            "turns": 0,
                            "input": 0,
                            "cached": 0,
                            "output": 0,
                            "thinking": 0,
                        },
                    )
                    p_entry["spend"] += cost
                    p_entry["turns"] += 1
                    p_entry["input"] += in_tok
                    p_entry["cached"] += cac_tok
                    p_entry["output"] += out_tok
                    p_entry["thinking"] += thk_tok
                    total_spend += cost
                    total_input += in_tok
                    total_cached += cac_tok
                    total_output += out_tok
                    total_thinking += thk_tok
                    key = (prov, m_name, a_name)
                    if key not in grouped:
                        grouped[key] = {
                            "turns": 0,
                            "in": 0,
                            "cac": 0,
                            "out": 0,
                            "thk": 0,
                            "cost": 0.0,
                        }
                    grouped[key]["turns"] += 1
                    grouped[key]["in"] += in_tok
                    grouped[key]["cac"] += cac_tok
                    grouped[key]["out"] += out_tok
                    grouped[key]["thk"] += thk_tok
                    grouped[key]["cost"] += cost
                    day_lbl = str(t.get("timestamp", ""))[:10] or time.strftime(
                        "%Y-%m-%d"
                    )
                    if day_lbl not in daily:
                        daily[day_lbl] = {"turns": 0, "tokens": 0, "cost": 0.0}
                    daily[day_lbl]["turns"] += 1
                    daily[day_lbl]["tokens"] += (
                        in_tok + cac_tok + out_tok + thk_tok
                    )
                    daily[day_lbl]["cost"] += cost
                matrix_rows = []
                for (prov, m_name, a_name), g in grouped.items():
                    tok_sum = g["in"] + g["cac"] + g["out"] + g["thk"]
                    matrix_rows.append(
                        {
                            "provider": prov,
                            "model_name": m_name,
                            "app_name": a_name,
                            "turns": g["turns"],
                            "input_tokens": g["in"],
                            "cached_tokens": g["cac"],
                            "output_tokens": g["out"],
                            "thinking_tokens": g["thk"],
                            "total_tokens": tok_sum,
                            "total_cost": round(g["cost"], 5),
                            "spend_share_pct": round(
                                (g["cost"] / total_spend * 100.0)
                                if total_spend > 0
                                else 0.0,
                                1,
                            ),
                        }
                    )
                matrix_rows.sort(key=lambda x: x["total_cost"], reverse=True)
                top_provider = (
                    max(provider_map.items(), key=lambda x: x[1]["spend"])[0]
                    if total_spend > 0
                    else "None"
                )
                top_spend = provider_map.get(top_provider, {}).get("spend", 0.0)
                total_tokens = (
                    total_input + total_cached + total_output + total_thinking
                )
                hypothetical_base = total_spend + savings_dollars
                savings_pct = (
                    (savings_dollars / hypothetical_base * 100.0)
                    if hypothetical_base > 0
                    else 0.0
                )
                blended_cost = (
                    (total_spend / total_tokens * 1_000_000.0)
                    if total_tokens > 0
                    else 0.0
                )
                top_share = (
                    (top_spend / total_spend * 100.0)
                    if total_spend > 0
                    else 0.0
                )
                timeline = []
                cum_cost = 0.0
                for d_lbl in sorted(daily.keys()):
                    d = daily[d_lbl]
                    cum_cost += d["cost"]
                    timeline.append(
                        {
                            "date": d_lbl,
                            "turns": d["turns"],
                            "daily_tokens": d["tokens"],
                            "daily_cost": round(d["cost"], 5),
                            "cumulative_cost": round(cum_cost, 5),
                        }
                    )
                return {
                    "status": "success",
                    "source": "local_traces",
                    "kpis": {
                        "total_spend": round(total_spend, 5),
                        "total_tokens": total_tokens,
                        "total_input": total_input,
                        "total_cached": total_cached,
                        "total_output": total_output,
                        "total_thinking": total_thinking,
                        "total_turns": len(local_turns),
                        "blended_cost_per_1m": round(blended_cost, 3),
                        "top_provider": top_provider,
                        "top_provider_share": round(top_share, 1),
                        "optimization_savings_dollars": round(
                            savings_dollars, 4
                        ),
                        "optimization_savings_pct": round(savings_pct, 1),
                    },
                    "provider_breakdown": provider_map,
                    "matrix_rows": matrix_rows,
                    "timeline": timeline,
                }
            friendly = TokenomicsWebHandler._format_friendly_bq_error(
                str(e), raw_proj, raw_ds, raw_tbl
            )
            return self._empty_finops_summary(
                str(e),
                extra={
                    "source": "bigquery",
                    "target": f"{raw_proj}.{raw_ds}.{raw_vw}"
                    if raw_proj
                    else f"{raw_ds}.{raw_vw}",
                    **friendly,
                },
            )

    def log_message(self, format: str, *args: Any) -> None:
        req_line = str(args[0]) if args else ""
        status_code = str(args[1]) if len(args) > 1 else ""
        if status_code == "200" and any(
            ep in req_line
            for ep in (
                "/agent-nexus/live_metrics.json",
                "/live_metrics.json",
                "/api/metrics",
                "/api/sessions",
            )
        ):
            return
        super().log_message(format, *args)

    def _get_bound_address(self) -> tuple[str, int]:
        srv = getattr(self, "server", None)
        addr = getattr(srv, "server_address", None)
        override_host = getattr(self, "bound_host", None)
        if isinstance(addr, tuple) and len(addr) >= 2:
            resolved_host = (
                str(override_host)
                if override_host
                else str(addr[0] or "127.0.0.1")
            )
            return resolved_host, int(addr[1])
        return str(override_host or "127.0.0.1"), 8080

    def _get_configured_allowed_host_rules(
        self,
    ) -> list[tuple[str, str, int | None]]:
        raw_entries: list[str] = list(
            getattr(self, "configured_allowed_hosts", ()) or ()
        )
        env_raw = os.environ.get("TOKENOMICS_ALLOWED_HOSTS")
        if env_raw:
            raw_entries.extend(env_raw.split(","))
        rules: list[tuple[str, str, int | None]] = []
        for entry in raw_entries:
            tok = str(entry or "").strip()
            if not tok:
                continue
            try:
                parsed = parse_allowed_host_pattern(tok)
            except ValueError as exc:
                logger.warning(
                    "Ignoring invalid allowed-host pattern %r: %s", tok, exc
                )
                continue
            if parsed not in rules:
                rules.append(parsed)
        return rules

    def _allowed_hosts_and_origins(self) -> tuple[set[str], set[str], int]:
        bound_host, bound_port = self._get_bound_address()
        allowed_hosts: set[str] = {"127.0.0.1", "localhost", "[::1]"}
        bh_clean = bound_host.strip().lower()
        if bh_clean and bh_clean not in ("0.0.0.0", "::", "[::]", ""):
            if ":" in bh_clean and not bh_clean.startswith("["):
                try:
                    ip_obj = ipaddress.IPv6Address(bh_clean)
                    allowed_hosts.add(f"[{ip_obj.compressed}]")
                except ValueError:
                    allowed_hosts.add(f"[{bh_clean}]")
            else:
                allowed_hosts.add(bh_clean)

        allowed_origins: set[str] = set()
        for h in allowed_hosts:
            allowed_origins.add(f"http://{h}:{bound_port}")
            if bound_port == 80:
                allowed_origins.add(f"http://{h}")
        return allowed_hosts, allowed_origins, bound_port

    def _evaluate_host_header(self) -> tuple[bool, str, set[str]]:
        headers = getattr(self, "headers", None)
        if not headers:
            return False, "Missing request headers.", set()
        raw_host = (headers.get("Host") or "").strip()
        host_part, port_part, parse_err = _parse_host_header(raw_host)
        if parse_err or host_part is None:
            return False, parse_err or "Malformed Host header.", set()

        default_hosts, default_origins, bound_port = (
            self._allowed_hosts_and_origins()
        )
        matched_any = False
        allowed_origins: set[str] = set()

        host_in_default = host_part in default_hosts
        if host_in_default and (port_part is None or port_part == bound_port):
            matched_any = True
            allowed_origins.update(default_origins)

        for (
            kind,
            target,
            rule_port,
        ) in self._get_configured_allowed_host_rules():
            if kind == "exact":
                host_ok = host_part == target
            else:
                host_ok = (
                    not host_part.startswith("[")
                    and host_part.endswith(target)
                    and len(host_part) > len(target)
                )
            if not host_ok:
                continue
            if rule_port is not None and port_part != rule_port:
                continue

            matched_any = True
            if port_part is None:
                allowed_origins.add(f"http://{host_part}")
                allowed_origins.add(f"https://{host_part}")
            else:
                allowed_origins.add(f"http://{host_part}:{port_part}")
                allowed_origins.add(f"https://{host_part}:{port_part}")
                if port_part == 80:
                    allowed_origins.add(f"http://{host_part}")
                elif port_part == 443:
                    allowed_origins.add(f"https://{host_part}")

        if not matched_any:
            if (
                host_in_default
                and port_part is not None
                and port_part != bound_port
            ):
                return False, f"Forbidden Host port: {port_part}", set()
            return False, f"Forbidden Host header: {raw_host!r}", set()

        return True, "", allowed_origins

    def _validate_host_header(self) -> tuple[bool, str]:
        ok, err_msg, _ = self._evaluate_host_header()
        return ok, err_msg

    def _get_allowed_cors_origin(self) -> str | None:
        headers = getattr(self, "headers", None)
        if not headers:
            return None
        raw_origin = headers.get("Origin")
        if not raw_origin:
            return None
        ok, _, allowed_origins = self._evaluate_host_header()
        if not ok:
            return None
        candidate = raw_origin.strip()
        if candidate.lower() in allowed_origins:
            return candidate
        return None

    def _validate_host_and_origin(self) -> str | None:
        ok, err_msg, allowed_origins = self._evaluate_host_header()
        if not ok:
            return err_msg

        headers = self.headers
        sec_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_site == "cross-site":
            return "Cross-site requests are forbidden."

        raw_origin = headers.get("Origin")
        if raw_origin is not None:
            if raw_origin.strip().lower() not in allowed_origins:
                return f"Forbidden Origin header: {raw_origin!r}"

        return None

    def _validate_json_content_type(self) -> str | None:
        headers = getattr(self, "headers", None)
        ctype_raw = (headers.get("Content-Type") or "") if headers else ""
        media_type = ctype_raw.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return "Content-Type must be application/json."
        return None

    def _parse_bounded_content_length(self) -> int | None:
        raw_cl = self.headers.get("Content-Length")
        if raw_cl is None or str(raw_cl).strip() == "":
            return 0
        try:
            cl = int(str(raw_cl).strip())
        except (TypeError, ValueError):
            self._send_json(
                400,
                {
                    "status": "error",
                    "message": "Invalid Content-Length header.",
                },
            )
            return None
        if cl < 0:
            self._send_json(
                400,
                {
                    "status": "error",
                    "message": "Negative Content-Length header.",
                },
            )
            return None
        if cl > self.MAX_REQUEST_BODY_BYTES:
            self._send_json(
                413,
                {
                    "status": "error",
                    "message": "Request payload exceeds 1 MiB limit.",
                },
            )
            return None
        return cl

    def _read_bounded_json_body(self) -> bytes | None:
        auth_err = self._validate_host_and_origin()
        if auth_err:
            self._send_json(403, {"status": "error", "message": auth_err})
            return None

        cl = self._parse_bounded_content_length()
        if cl is None:
            return None

        ctype_err = self._validate_json_content_type()
        if ctype_err:
            self._send_json(403, {"status": "error", "message": ctype_err})
            return None

        if cl == 0:
            return b"{}"
        try:
            data = self.rfile.read(cl)
        except TimeoutError:
            self._send_json(
                408,
                {"status": "error", "message": "Request body read timed out."},
            )
            return None
        if len(data) < cl:
            self._send_json(
                400, {"status": "error", "message": "Incomplete request body."}
            )
            return None
        return data

    @staticmethod
    def _reject_json_constant(const_str: str) -> Any:
        raise ValueError(
            f"Non-RFC8259 JSON constant {const_str!r} is not allowed."
        )

    def _send_json(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        allowed_origin = self._get_allowed_cors_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        auth_err = self._validate_host_and_origin()
        if auth_err:
            self._send_json(403, {"status": "error", "message": auth_err})
            return
        self.send_response(200, "ok")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE"
        )
        self.send_header(
            "Access-Control-Allow-Headers", "X-Requested-With, Content-Type"
        )
        self.end_headers()

    def do_HEAD(self):
        host_ok, host_err = self._validate_host_header()
        if not host_ok:
            self._send_json(403, {"status": "error", "message": host_err})
            return
        self.do_GET()

    def do_GET(self):
        host_ok, host_err = self._validate_host_header()
        if not host_ok:
            self._send_json(403, {"status": "error", "message": host_err})
            return

        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        if path.startswith("/api/") or path in (
            "/agent-nexus/live_metrics.json",
            "/live_metrics.json",
        ):
            sec_site = (
                (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            )
            if sec_site == "cross-site":
                self._send_json(
                    403,
                    {
                        "status": "error",
                        "message": "Cross-site requests are forbidden.",
                    },
                )
                return

        if path in [
            "/agent-nexus/live_metrics.json",
            "/live_metrics.json",
            "/api/metrics",
        ]:
            session_id = query_params.get("session_id", ["global"])[0]
            metrics_data = self.fetch_metrics(session_id=session_id)
            self._send_json(200, metrics_data)
            return

        elif path == "/api/sessions":
            sessions = self.fetch_sessions()
            self._send_json(200, sessions)
            return

        elif path == "/api/models":
            models_cfg = dict(self.load_models_config())
            models_cfg["writable_config_path"] = (
                get_writable_models_config_path()
            )
            self._send_json(200, models_cfg)
            return

        elif path in ["/api/config", "/api/bq/config"]:
            cfg = self.get_bq_config()
            self._send_json(200, cfg)
            return

        elif path == "/api/bq/stats":
            stats = self.fetch_bq_stats()
            self._send_json(200, stats)
            return

        elif path == "/api/bq/logs":
            raw_limit = query_params.get("limit", ["50"])[0]
            raw_offset = query_params.get("offset", ["0"])[0]
            try:
                parsed_limit = int(raw_limit)
                parsed_offset = int(raw_offset)
            except (TypeError, ValueError):
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "Invalid 'limit' or 'offset' query parameter.",
                    },
                )
                return
            if parsed_limit < 1 or parsed_offset < 0:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "'limit' must be >= 1 and 'offset' must be >= 0.",
                    },
                )
                return
            limit = min(500, max(1, parsed_limit))
            offset = max(0, parsed_offset)
            app_name = query_params.get("app_name", ["all"])[0]
            search = query_params.get("search", [""])[0]
            logs = self.fetch_bq_explorer_logs(
                limit=limit, offset=offset, app_name=app_name, search=search
            )
            self._send_json(200, logs)
            return

        elif path == "/api/finops/summary":
            timeframe = query_params.get("timeframe", ["all"])[0]
            provider = query_params.get("provider", ["all"])[0]
            summary = self.fetch_finops_summary(
                timeframe=timeframe, provider=provider
            )
            self._send_json(200, summary)
            return

        if (
            path.startswith("/dev-ui")
            or path.startswith("/api/apps")
            or path.startswith("/api/events")
        ):
            self.proxy_to_adk()
            return

        if path == "/" or not os.path.exists(
            os.path.join(WEB_DIR, path.lstrip("/"))
        ):
            self.path = "/index.html"
        if getattr(self, "command", "GET") == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == "/api/bq/config":
            post_data = self._read_bounded_json_body()
            if post_data is None:
                return
            try:
                payload = json.loads(
                    post_data.decode("utf-8"),
                    parse_constant=self._reject_json_constant,
                )
                updated_cfg = self.set_bq_config(payload)
                self._send_json(
                    200, {"status": "success", "config": updated_cfg}
                )
            except Exception as e:
                self._send_json(400, {"status": "error", "message": str(e)})
            return

        if path == "/api/models":
            post_data = self._read_bounded_json_body()
            if post_data is None:
                return
            try:
                model_entry = json.loads(
                    post_data.decode("utf-8"),
                    parse_constant=self._reject_json_constant,
                )
                if (
                    not isinstance(model_entry, dict)
                    or not str(model_entry.get("id") or "").strip()
                ):
                    raise ValueError(
                        "Model configuration requires a non-empty 'id'."
                    )
                saved_cfg = self.upsert_model_entry(model_entry)
                target_canon = canonicalize_model_id(
                    str(model_entry.get("id") or "")
                )
                saved_model = next(
                    (
                        m
                        for m in saved_cfg.get("models", [])
                        if m.get("id") == target_canon
                    ),
                    self._normalize_model_entry(model_entry),
                )
                resp_body: dict[str, Any] = {
                    "status": "success",
                    "model": saved_model,
                    "models": saved_cfg.get("models", []),
                    "config_path": saved_cfg.get(
                        "config_path", get_models_config_path()
                    ),
                    "models_config_path": saved_cfg.get(
                        "models_config_path", get_models_config_path()
                    ),
                    "writable_config_path": saved_cfg.get(
                        "writable_config_path",
                        get_writable_models_config_path(),
                    ),
                }
                if "deleted_ids" in saved_cfg:
                    resp_body["deleted_ids"] = saved_cfg["deleted_ids"]
                if saved_cfg.get("replace_defaults"):
                    resp_body["replace_defaults"] = True
                self._send_json(200, resp_body)
            except Exception as e:
                self._send_json(400, {"status": "error", "message": str(e)})
            return

        if path == "/api/models_config_raw":
            post_data = self._read_bounded_json_body()
            if post_data is None:
                return
            try:
                payload = json.loads(
                    post_data.decode("utf-8"),
                    parse_constant=self._reject_json_constant,
                )
                if isinstance(payload, dict) and "raw_json" in payload:
                    raw_val = payload["raw_json"]
                    parsed_cfg = (
                        json.loads(
                            raw_val, parse_constant=self._reject_json_constant
                        )
                        if isinstance(raw_val, str)
                        else raw_val
                    )
                    if isinstance(parsed_cfg, dict):
                        if (
                            "deleted_ids" in payload
                            and "deleted_ids" not in parsed_cfg
                        ):
                            parsed_cfg["deleted_ids"] = payload["deleted_ids"]
                        if (
                            "replace_defaults" in payload
                            and "replace_defaults" not in parsed_cfg
                        ):
                            parsed_cfg["replace_defaults"] = payload[
                                "replace_defaults"
                            ]
                else:
                    parsed_cfg = payload
                if isinstance(parsed_cfg, list):
                    parsed_cfg = {"models": parsed_cfg}
                if not isinstance(parsed_cfg, dict) or not isinstance(
                    parsed_cfg.get("models"), list
                ):
                    raise ValueError(
                        "JSON root must be an object containing a 'models' array."
                    )
                saved_cfg = self.save_models_config(parsed_cfg)
                raw_resp: dict[str, Any] = {
                    "status": "success",
                    "models": saved_cfg.get("models", []),
                    "config_path": saved_cfg.get(
                        "config_path", get_models_config_path()
                    ),
                    "models_config_path": saved_cfg.get(
                        "models_config_path", get_models_config_path()
                    ),
                    "writable_config_path": saved_cfg.get(
                        "writable_config_path",
                        get_writable_models_config_path(),
                    ),
                }
                if "deleted_ids" in saved_cfg:
                    raw_resp["deleted_ids"] = saved_cfg["deleted_ids"]
                if saved_cfg.get("replace_defaults"):
                    raw_resp["replace_defaults"] = True
                self._send_json(200, raw_resp)
            except Exception as e:
                self._send_json(400, {"status": "error", "message": str(e)})
            return

        if (
            path.startswith("/api/run")
            or path.startswith("/api/apps")
            or path.startswith("/api/events")
        ):
            self.proxy_to_adk()
            return

        auth_err = self._validate_host_and_origin()
        if auth_err:
            self._send_json(403, {"status": "error", "message": auth_err})
            return
        self._send_json(
            404, {"status": "error", "message": f"Unknown endpoint: {path}"}
        )

    def do_DELETE(self):
        auth_err = self._validate_host_and_origin()
        if auth_err:
            self._send_json(403, {"status": "error", "message": auth_err})
            return
        ctype_err = self._validate_json_content_type()
        if ctype_err:
            self._send_json(403, {"status": "error", "message": ctype_err})
            return

        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        if path == "/api/models":
            model_id = query_params.get("id", [""])[0].strip()
            if not model_id:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "Missing 'id' query parameter.",
                    },
                )
                return
            try:
                saved_cfg = self.delete_model_entry(model_id)
                del_resp: dict[str, Any] = {
                    "status": "success",
                    "deleted_id": canonicalize_model_id(model_id),
                    "models": saved_cfg.get("models", []),
                    "config_path": saved_cfg.get(
                        "config_path", get_models_config_path()
                    ),
                    "models_config_path": saved_cfg.get(
                        "models_config_path", get_models_config_path()
                    ),
                    "writable_config_path": saved_cfg.get(
                        "writable_config_path",
                        get_writable_models_config_path(),
                    ),
                }
                if "deleted_ids" in saved_cfg:
                    del_resp["deleted_ids"] = saved_cfg["deleted_ids"]
                if saved_cfg.get("replace_defaults"):
                    del_resp["replace_defaults"] = True
                self._send_json(200, del_resp)
            except Exception as e:
                self._send_json(400, {"status": "error", "message": str(e)})
            return

        self._send_json(
            404, {"status": "error", "message": f"Unknown endpoint: {path}"}
        )

    def proxy_to_adk(self):
        is_mutating = self.command not in ("GET", "HEAD", "OPTIONS")
        if is_mutating:
            auth_err = self._validate_host_and_origin()
            if auth_err:
                self._send_json(403, {"status": "error", "message": auth_err})
                return
        else:
            host_ok, host_err = self._validate_host_header()
            if not host_ok:
                self._send_json(403, {"status": "error", "message": host_err})
                return

        cl = self._parse_bounded_content_length()
        if cl is None:
            return

        if is_mutating:
            ctype_err = self._validate_json_content_type()
            if ctype_err:
                self._send_json(403, {"status": "error", "message": ctype_err})
                return

        target_url = f"http://127.0.0.1:{self.adk_proxy_port}{self.path}"
        try:
            req_headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ["host", "accept-encoding"]
            }
            req_headers["Accept-Encoding"] = "identity"
            body = None
            if cl > 0:
                try:
                    body = self.rfile.read(cl)
                except TimeoutError:
                    self._send_json(
                        408,
                        {
                            "status": "error",
                            "message": "Request body read timed out.",
                        },
                    )
                    return

            req = urllib.request.Request(
                target_url, data=body, headers=req_headers, method=self.command
            )
            with urllib.request.urlopen(req, timeout=35) as resp:
                self.send_response(resp.status)
                resp_body = resp.read()
                for k, v in resp.headers.items():
                    if k.lower() not in [
                        "transfer-encoding",
                        "content-length",
                        "content-encoding",
                        "access-control-allow-origin",
                    ]:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in [
                    "transfer-encoding",
                    "content-length",
                    "content-encoding",
                    "access-control-allow-origin",
                ]:
                    self.send_header(k, v)
            err_body = e.read()
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            if self.path.startswith("/dev-ui"):
                local_turns = self._load_local_trace_turns()
                raw_trace_label = (
                    getattr(self, "traces_path", None) or "artifacts/traces/"
                )
                safe_trace_label = html.escape(str(raw_trace_label), quote=True)
                t_count = len(local_turns)
                t_cost = sum(
                    float(t.get("total_cost", 0.0)) for t in local_turns
                )
                t_tok = sum(
                    int(t.get("prompt_tokens", 0))
                    + int(t.get("output_tokens", 0))
                    + int(t.get("thinking_tokens", 0))
                    for t in local_turns
                )
                page_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin:0; font-family: system-ui, -apple-system, sans-serif; background:#0f172a; color:#f8fafc; display:flex; align-items:center; justify-content:center; height:100vh; }}
.card {{ max-width:640px; padding:2.2rem; border-radius:16px; background:rgba(30,41,59,0.85); border:1px solid rgba(96,165,250,0.3); box-shadow:0 20px 50px rgba(0,0,0,0.4); text-align:center; }}
h2 {{ margin:0 0 0.5rem; color:#60a5fa; font-size:1.4rem; }}
p {{ color:#94a3b8; font-size:0.95rem; line-height:1.5; margin:0.6rem 0; }}
.stats {{ display:flex; justify-content:center; gap:1.5rem; margin:1.4rem 0; padding:1rem; background:rgba(15,23,42,0.7); border-radius:10px; border:1px solid rgba(255,255,255,0.08); }}
.stat-val {{ font-size:1.25rem; font-weight:700; color:#34d399; font-family:monospace; }}
.stat-lbl {{ font-size:0.75rem; color:#94a3b8; text-transform:uppercase; margin-top:0.2rem; }}
.btn {{ display:inline-block; margin:0.4rem; padding:0.65rem 1.2rem; border-radius:8px; font-weight:600; font-size:0.9rem; cursor:pointer; border:none; background:#3b82f6; color:#fff; }}
.btn-sec {{ background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.4); }}
code {{ background:rgba(0,0,0,0.4); padding:0.2rem 0.45rem; border-radius:5px; color:#e2e8f0; font-size:0.85rem; }}
</style></head><body>
<div class="card">
  <h2>⚡ Local Trace Observatory Active</h2>
  <p>Inspecting trace file: <code>{safe_trace_label}</code></p>
  <div class="stats">
    <div><div class="stat-val">{t_count}</div><div class="stat-lbl">Recorded Turns</div></div>
    <div><div class="stat-val">{t_tok:,}</div><div class="stat-lbl">Total Tokens</div></div>
    <div><div class="stat-val">${t_cost:.5f}</div><div class="stat-lbl">Total Spend</div></div>
  </div>
  <div>
    <button class="btn" onclick="window.parent.document.getElementById('tab-btn-telemetry').click()">📊 Open Telemetry &amp; Waterfall</button>
    <button class="btn btn-sec" onclick="window.parent.document.getElementById('tab-btn-bq').click()">🗄️ Open Trace Log Explorer</button>
  </div>
  <p style="margin-top:1.2rem; font-size:0.82rem;">To enable live chat in this pane, run <code>adk web . --port 8082</code> in a separate terminal.</p>
</div></body></html>"""
                body_bytes = page_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                if getattr(self, "command", "GET") != "HEAD":
                    self.wfile.write(body_bytes)
                return
            self._send_json(502, {"error": "Bad Gateway", "details": str(e)})

    @staticmethod
    def _parse_valid_rate(raw_val: Any, field_name: str) -> float:
        if isinstance(raw_val, bool) or raw_val is None:
            raise ValueError(
                f"Invalid rate for '{field_name}': must be a finite number in [0.0, 10000.0]."
            )
        if isinstance(raw_val, str) and not raw_val.strip():
            raise ValueError(f"Invalid rate for '{field_name}': empty string.")
        try:
            val = float(raw_val)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid rate for '{field_name}': {raw_val!r}"
            ) from exc
        if not math.isfinite(val) or val < 0.0 or val > 10000.0:
            raise ValueError(
                f"Rate '{field_name}' out of bounds ({val}): must be finite and in [0.0, 10000.0]."
            )
        return val

    @staticmethod
    def _normalize_model_entry(entry: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(entry, dict):
            raise ValueError("Model configuration entry must be a JSON object.")
        m = dict(entry)
        raw_m_id = str(m.get("id") or "").strip()
        if not raw_m_id:
            raise ValueError("Model configuration requires a non-empty 'id'.")
        m_id = canonicalize_model_id(raw_m_id)
        m["id"] = m_id
        m["name"] = str(m.get("name") or m_id.split("/")[-1] or m_id).strip()
        m["provider"] = str(m.get("provider") or "google").strip().lower()
        raw_p = m.get("pricing")
        if raw_p is not None and not isinstance(raw_p, dict):
            raise ValueError("Model 'pricing' field must be an object.")
        p = dict(raw_p or {})

        if "input" in p:
            raw_in = p["input"]
        elif "input_per_million" in p:
            raw_in = p["input_per_million"]
        elif "input_rate" in m:
            raw_in = m["input_rate"]
        else:
            raw_in = 0.75
        in_r = TokenomicsWebHandler._parse_valid_rate(raw_in, "input")

        if "cached" in p:
            raw_cac = p["cached"]
        elif "cached_per_million" in p:
            raw_cac = p["cached_per_million"]
        elif "cached_rate" in m:
            raw_cac = m["cached_rate"]
        else:
            raw_cac = round(in_r * 0.25, 6)
        cac_r = TokenomicsWebHandler._parse_valid_rate(raw_cac, "cached")

        if "output" in p:
            raw_out = p["output"]
        elif "output_per_million" in p:
            raw_out = p["output_per_million"]
        elif "output_rate" in m:
            raw_out = m["output_rate"]
        else:
            raw_out = 3.75
        out_r = TokenomicsWebHandler._parse_valid_rate(raw_out, "output")

        m["pricing"] = {
            "input": in_r,
            "cached": cac_r,
            "output": out_r,
            "input_per_million": in_r,
            "cached_per_million": cac_r,
            "output_per_million": out_r,
        }
        raw_tb = m.get("thinking_budget", 0)
        if isinstance(raw_tb, int) and not isinstance(raw_tb, bool):
            m["thinking_budget"] = raw_tb
        else:
            tb_str = str(raw_tb if raw_tb is not None else 0).strip()
            try:
                m["thinking_budget"] = int(tb_str)
            except ValueError:
                low_tb = tb_str.lower()
                m["thinking_budget"] = (
                    low_tb
                    if low_tb in ("off", "low", "medium", "high", "dynamic")
                    else (tb_str or 0)
                )
        try:
            m["max_output_tokens"] = int(
                m.get("max_output_tokens", 8192) or 8192
            )
        except (ValueError, TypeError):
            m["max_output_tokens"] = 8192
        return m

    @staticmethod
    def _sanitize_deleted_ids(
        raw_deleted: Any, exclude_ids: set[str] | None = None
    ) -> list[str]:
        if not isinstance(raw_deleted, list):
            return []
        excluded_lower = {x.lower() for x in (exclude_ids or set())}
        cleaned: list[str] = []
        seen_lower: set[str] = set()
        for item in raw_deleted:
            if not isinstance(item, str) or not item.strip():
                continue
            canon = canonicalize_model_id(item.strip())
            c_low = canon.lower()
            if c_low in excluded_lower or c_low in seen_lower:
                continue
            seen_lower.add(c_low)
            cleaned.append(canon)
        return cleaned

    @classmethod
    def _load_sanitized_models_config(cls) -> dict[str, Any]:
        with _MODELS_CONFIG_LOCK:
            cfg_path = get_models_config_path()
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, encoding="utf-8") as f:
                        data = json.load(
                            f, parse_constant=cls._reject_json_constant
                        )
                    if isinstance(data, dict) and (
                        isinstance(data.get("models"), list)
                        or isinstance(data.get("deleted_ids"), list)
                        or "replace_defaults" in data
                    ):
                        replace_defaults = bool(data.get("replace_defaults"))
                        has_models_key = isinstance(data.get("models"), list)
                        valid_models: list[dict[str, Any]] = []
                        if has_models_key:
                            for idx, m in enumerate(data["models"]):
                                if not isinstance(m, dict):
                                    logger.warning(
                                        "Skipping non-object model entry at index %d in %s.",
                                        idx,
                                        cfg_path,
                                    )
                                    continue
                                raw_id = str(m.get("id") or "").strip()
                                if not raw_id:
                                    logger.warning(
                                        "Skipping model entry at index %d in %s: missing non-empty 'id'.",
                                        idx,
                                        cfg_path,
                                    )
                                    continue
                                try:
                                    valid_models.append(
                                        cls._normalize_model_entry(m)
                                    )
                                except (ValueError, TypeError) as exc:
                                    logger.warning(
                                        "Skipping invalid model entry %r at index %d in %s: %s",
                                        raw_id,
                                        idx,
                                        cfg_path,
                                        exc,
                                    )
                                    continue
                        present_ids = {m["id"] for m in valid_models}
                        deleted_ids = cls._sanitize_deleted_ids(
                            data.get("deleted_ids"), exclude_ids=present_ids
                        )
                        if not has_models_key and not replace_defaults:
                            del_low = {d.lower() for d in deleted_ids}
                            valid_models = [
                                cls._normalize_model_entry(dm)
                                for dm in DEFAULT_MODELS_CONFIG.get(
                                    "models", []
                                )
                                if isinstance(dm, dict)
                                and canonicalize_model_id(
                                    str(dm.get("id") or "")
                                ).lower()
                                not in del_low
                            ]
                        out: dict[str, Any] = dict(data)
                        out["models"] = valid_models
                        if deleted_ids:
                            out["deleted_ids"] = deleted_ids
                        elif "deleted_ids" in out:
                            out.pop("deleted_ids", None)
                        if replace_defaults:
                            out["replace_defaults"] = True
                        out["config_path"] = cfg_path
                        out["models_config_path"] = cfg_path
                        return out
                    logger.warning(
                        "Invalid models_config schema in %s (expected JSON object with 'models' list); falling back to packaged defaults.",
                        cfg_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to parse models_config at %s (%s); falling back to packaged defaults.",
                        cfg_path,
                        exc,
                    )
            return {
                "models": [
                    cls._normalize_model_entry(m)
                    for m in DEFAULT_MODELS_CONFIG.get("models", [])
                    if isinstance(m, dict)
                ],
                "config_path": cfg_path,
                "models_config_path": cfg_path,
            }

    @classmethod
    def _build_sanitized_pricing_engine(cls) -> PricingEngine:
        sanitized = cls._load_sanitized_models_config()
        replace_defaults = bool(sanitized.get("replace_defaults"))
        deleted_ids = cls._sanitize_deleted_ids(sanitized.get("deleted_ids"))
        merged_by_id: dict[str, dict[str, Any]] = {}

        if not replace_defaults:
            for dm in DEFAULT_MODELS_CONFIG.get("models", []):
                if isinstance(dm, dict) and dm.get("id"):
                    norm_dm = cls._normalize_model_entry(dm)
                    merged_by_id[norm_dm["id"]] = norm_dm
            if os.path.exists(_PACKAGED_MODELS_CFG):
                try:
                    with open(_PACKAGED_MODELS_CFG, encoding="utf-8") as pf:
                        pkg_data = json.load(pf)
                    if isinstance(pkg_data, dict):
                        for pm in pkg_data.get("models", []):
                            if isinstance(pm, dict) and pm.get("id"):
                                norm_pm = cls._normalize_model_entry(pm)
                                merged_by_id[norm_pm["id"]] = norm_pm
                except Exception:
                    pass
            # Purge deleted_ids from seeded defaults ONLY (never entries present in user sanitized['models'])
            del_lower = {d.lower() for d in deleted_ids}
            for existing_k in list(merged_by_id.keys()):
                if (
                    existing_k.lower() in del_lower
                    or canonicalize_model_id(existing_k).lower() in del_lower
                ):
                    merged_by_id.pop(existing_k, None)

        for m in sanitized.get("models", []):
            if (
                not isinstance(m, dict)
                or not m.get("id")
                or not isinstance(m.get("pricing"), dict)
            ):
                continue
            canon_m_id = canonicalize_model_id(str(m["id"]))
            base_entry = merged_by_id.get(canon_m_id)
            merged_entry = dict(base_entry) if base_entry else {}
            merged_entry.update(m)
            merged_entry["id"] = canon_m_id
            merged_by_id[canon_m_id] = merged_entry

        cfg = TokenomicsConfig(models_config_path=_PACKAGED_MODELS_CFG)
        frozen_models = [copy.deepcopy(v) for v in merged_by_id.values()]

        def _get_frozen_models_config() -> dict[str, Any]:
            return {"models": copy.deepcopy(frozen_models)}

        cfg.get_models_config = _get_frozen_models_config  # type: ignore[method-assign]
        return PricingEngine(cfg)

    def load_models_config(self) -> dict[str, Any]:
        return self._load_sanitized_models_config()

    def save_models_config(self, cfg_data: dict[str, Any]) -> dict[str, Any]:
        with _MODELS_CONFIG_LOCK:
            if not isinstance(cfg_data, dict):
                raise ValueError("Configuration data must be a JSON object.")
            target_path = get_writable_models_config_path()
            if os.path.abspath(target_path) == os.path.abspath(
                _PACKAGED_MODELS_CFG
            ):
                raise RuntimeError(
                    "Refusing to overwrite packaged models_config.json."
                )

            existing_disk: dict[str, Any] = {}
            if os.path.exists(target_path):
                try:
                    with open(target_path, encoding="utf-8") as ef:
                        loaded_existing = json.load(ef)
                    if isinstance(loaded_existing, dict):
                        existing_disk = loaded_existing
                except Exception:
                    existing_disk = {}

            source_models = cfg_data.get("models")
            if source_models is None and not os.path.exists(target_path):
                base_path = resolve_models_config_path()
                if os.path.exists(base_path):
                    with open(base_path, encoding="utf-8") as bf:
                        base_loaded = json.load(bf)
                    source_models = base_loaded.get("models", [])
                else:
                    source_models = DEFAULT_MODELS_CONFIG.get("models", [])
            elif source_models is None:
                source_models = existing_disk.get("models", [])

            if not isinstance(source_models, list):
                raise ValueError("'models' must be a list.")

            models_list: list[dict[str, Any]] = []
            seen_pos: dict[str, int] = {}
            for raw_m in source_models:
                if not isinstance(raw_m, dict):
                    raise ValueError("Each model entry must be a JSON object.")
                norm_m = self._normalize_model_entry(raw_m)
                key_low = norm_m["id"].lower()
                if key_low in seen_pos:
                    models_list[seen_pos[key_low]] = norm_m
                else:
                    seen_pos[key_low] = len(models_list)
                    models_list.append(norm_m)

            present_ids = {m["id"] for m in models_list}
            raw_deleted_src = (
                cfg_data["deleted_ids"]
                if "deleted_ids" in cfg_data
                else existing_disk.get("deleted_ids")
            )
            deleted_ids = self._sanitize_deleted_ids(
                raw_deleted_src, exclude_ids=present_ids
            )
            replace_defaults = (
                bool(cfg_data["replace_defaults"])
                if "replace_defaults" in cfg_data
                else bool(existing_disk.get("replace_defaults"))
            )

            normalized_cfg: dict[str, Any] = {"models": models_list}
            if deleted_ids:
                normalized_cfg["deleted_ids"] = deleted_ids
            if replace_defaults:
                normalized_cfg["replace_defaults"] = True

            target_dir = os.path.dirname(os.path.abspath(target_path))
            os.makedirs(target_dir, exist_ok=True)

            tmp_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target_dir,
                    prefix=".models_config_",
                    suffix=".tmp",
                    delete=False,
                ) as tmp_f:
                    tmp_name = tmp_f.name
                    json.dump(normalized_cfg, tmp_f, indent=2, allow_nan=False)
                    tmp_f.flush()
                    os.fsync(tmp_f.fileno())
                os.replace(tmp_name, target_path)
                tmp_name = None
            finally:
                if tmp_name and os.path.exists(tmp_name):
                    try:
                        os.remove(tmp_name)
                    except OSError:
                        pass

            engine = self._build_sanitized_pricing_engine()
            TokenomicsWebHandler.pricing = engine
            TokenomicsWebHandler._pricing_config_sig = (
                TokenomicsWebHandler._stat_models_config_sig()
            )
            self.pricing = engine
            self._clear_bq_cache()

            result_cfg = dict(normalized_cfg)
            result_cfg["config_path"] = target_path
            result_cfg["models_config_path"] = target_path
            result_cfg["writable_config_path"] = target_path
            return result_cfg

    @staticmethod
    def _merge_pricing_dict(
        existing_pricing: Mapping[str, Any] | None,
        incoming_pricing: Mapping[str, Any],
    ) -> dict[str, Any]:
        base_p = (
            dict(existing_pricing)
            if isinstance(existing_pricing, Mapping)
            else {}
        )
        inc_p = dict(incoming_pricing)
        rate_keys = (
            "input",
            "input_per_million",
            "cached",
            "cached_per_million",
            "output",
            "output_per_million",
        )
        merged_p: dict[str, Any] = {
            k: v for k, v in base_p.items() if k not in rate_keys
        }
        for k, v in inc_p.items():
            if k not in rate_keys:
                merged_p[k] = v
        for short_k, per_m_k in (
            ("input", "input_per_million"),
            ("cached", "cached_per_million"),
            ("output", "output_per_million"),
        ):
            if short_k in inc_p:
                val = inc_p[short_k]
            elif per_m_k in inc_p:
                val = inc_p[per_m_k]
            elif short_k in base_p:
                val = base_p[short_k]
            elif per_m_k in base_p:
                val = base_p[per_m_k]
            else:
                continue
            merged_p[short_k] = val
            merged_p[per_m_k] = val
        return merged_p

    def upsert_model_entry(self, model_entry: dict[str, Any]) -> dict[str, Any]:
        with _MODELS_CONFIG_LOCK:
            if not isinstance(model_entry, dict):
                raise ValueError(
                    "Model configuration entry must be a JSON object."
                )
            raw_id = str(model_entry.get("id") or "").strip()
            if not raw_id:
                raise ValueError(
                    "Model configuration requires a non-empty 'id'."
                )
            target_id = canonicalize_model_id(raw_id)
            target_low = target_id.lower()

            cfg = self.load_models_config()
            models = list(cfg.get("models") or [])
            deleted_ids = [
                d
                for d in self._sanitize_deleted_ids(cfg.get("deleted_ids"))
                if d.lower() != target_low and d.lower() != raw_id.lower()
            ]
            replace_defaults = bool(cfg.get("replace_defaults"))

            updated = False
            for idx, existing in enumerate(models):
                ex_id = canonicalize_model_id(str(existing.get("id") or ""))
                if ex_id.lower() == target_low:
                    merged_raw = dict(existing)
                    merged_raw.update(model_entry)
                    merged_raw["id"] = target_id
                    if "pricing" in model_entry and isinstance(
                        model_entry["pricing"], dict
                    ):
                        merged_raw["pricing"] = self._merge_pricing_dict(
                            existing.get("pricing"), model_entry["pricing"]
                        )
                    norm_entry = self._normalize_model_entry(merged_raw)
                    models[idx] = norm_entry
                    updated = True
                    break

            if not updated:
                seeded_base = next(
                    (
                        dm
                        for dm in DEFAULT_MODELS_CONFIG.get("models", [])
                        if isinstance(dm, dict)
                        and canonicalize_model_id(
                            str(dm.get("id") or "")
                        ).lower()
                        == target_low
                    ),
                    None,
                )
                merged_new = dict(seeded_base) if seeded_base else {}
                merged_new.update(model_entry)
                merged_new["id"] = target_id
                if (
                    seeded_base
                    and "pricing" in model_entry
                    and isinstance(model_entry["pricing"], dict)
                ):
                    merged_new["pricing"] = self._merge_pricing_dict(
                        seeded_base.get("pricing"), model_entry["pricing"]
                    )
                models.append(self._normalize_model_entry(merged_new))

            save_payload: dict[str, Any] = {
                "models": models,
                "deleted_ids": deleted_ids,
            }
            if replace_defaults:
                save_payload["replace_defaults"] = True
            return self.save_models_config(save_payload)

    def delete_model_entry(self, model_id: str) -> dict[str, Any]:
        with _MODELS_CONFIG_LOCK:
            canon_target = canonicalize_model_id(model_id)
            canon_low = canon_target.lower()
            raw_low = str(model_id or "").strip().lower()
            cfg = self.load_models_config()
            removed_canon_ids: list[str] = []
            models: list[dict[str, Any]] = []
            for m in cfg.get("models") or []:
                m_id_raw = str(m.get("id") or "").strip()
                m_id_canon = canonicalize_model_id(m_id_raw)
                m_name = str(m.get("name") or "").strip()
                if (
                    m_id_raw.lower() == raw_low
                    or m_id_canon.lower() == canon_low
                    or m_name == model_id
                ):
                    removed_canon_ids.append(m_id_canon)
                    continue
                models.append(m)

            deleted_ids = list(
                self._sanitize_deleted_ids(cfg.get("deleted_ids"))
            )
            for did in [canon_target, *removed_canon_ids]:
                if did and all(d.lower() != did.lower() for d in deleted_ids):
                    deleted_ids.append(did)

            save_payload: dict[str, Any] = {
                "models": models,
                "deleted_ids": deleted_ids,
            }
            if cfg.get("replace_defaults"):
                save_payload["replace_defaults"] = True
            return self.save_models_config(save_payload)

    def _load_local_trace_turns(self) -> list[dict[str, Any]]:
        traces = []
        search_paths = []
        explicit_trace = getattr(self, "traces_path", None)
        if isinstance(explicit_trace, str) and explicit_trace.strip():
            search_paths.append(explicit_trace.strip())
        else:
            search_paths.extend(
                [
                    "artifacts/traces/*.json",
                    "agent-nexus/artifacts/traces/*.json",
                ]
            )

        target_files = []
        seen_files = set()
        raw_export = os.environ.get("TOKENOMICS_AUTO_EXPORT_PATH")
        env_export_base = os.path.basename(raw_export) if raw_export else ""
        known_live_names = {
            "live_adk_session.json",
            "session.json",
            "ci_eval.json",
        }
        if env_export_base:
            known_live_names.add(env_export_base)
        for p in search_paths:
            if not p:
                continue
            candidates = []
            if os.path.isfile(p):
                candidates.append(p)
            elif os.path.isdir(p):
                candidates.extend(sorted(glob.glob(os.path.join(p, "*.json"))))
            elif (
                os.path.basename(p) in known_live_names
                and os.path.dirname(p)
                and os.path.isdir(os.path.dirname(p))
            ):
                candidates.extend(
                    sorted(
                        glob.glob(os.path.join(os.path.dirname(p), "*.json"))
                    )
                )
            else:
                candidates.extend(sorted(glob.glob(p)))
            for c in candidates:
                abs_c = os.path.abspath(c)
                if abs_c not in seen_files:
                    seen_files.add(abs_c)
                    target_files.append(c)

        pricing = (
            self._get_active_pricing_engine()
            if hasattr(self, "_get_active_pricing_engine")
            else (getattr(self, "pricing", None) or PricingEngine())
        )
        models_cfg = self.load_models_config()
        for tf in target_files:
            try:
                with open(tf, encoding="utf-8") as f:
                    data = json.load(f)
                raw_turns = []
                if isinstance(data, list):
                    raw_turns = data
                elif isinstance(data, dict):
                    if "turns" in data and isinstance(data["turns"], list):
                        raw_turns = data["turns"]
                    elif (
                        "eval_case_results" in data
                        or "evalCaseResults" in data
                        or "invocations" in data
                    ):
                        from adk_tokenomics_plugin.cli_profiler import (
                            parse_trace_or_eval_payload,
                        )

                        raw_turns = parse_trace_or_eval_payload(data)
                    elif "eval_cases" in data:
                        for case in data["eval_cases"]:
                            p_text = "".join(
                                part.get("text", "")
                                for part in case.get("prompt", {}).get(
                                    "parts", []
                                )
                            )
                            r_text = "".join(
                                part.get("text", "")
                                for r in case.get("responses", [])
                                for part in r.get("response", {}).get(
                                    "parts", []
                                )
                            )
                            inst_len = sum(
                                len(a.get("instruction", ""))
                                for a in case.get("agent_data", {})
                                .get("agents", {})
                                .values()
                            )
                            est_p = max(10, (len(p_text) + inst_len) // 4)
                            est_o = max(5, len(r_text) // 4)
                            raw_turns.append(
                                {
                                    "app_name": "eval_suite",
                                    "prompt_tokens": est_p,
                                    "cached_tokens": 0,
                                    "output_tokens": est_o,
                                    "thinking_tokens": 0,
                                    "timestamp": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ"
                                    ),
                                    "session_id": "session_local_eval",
                                    "user_query": p_text.strip()
                                    or "Eval Prompt",
                                    "agent_response": r_text.strip()
                                    or "Response",
                                    "model_name": "publishers/google/models/gemini-3.8-flash",
                                }
                            )
                active_prompt_by_session: dict[str, str] = {}
                for rt in raw_turns:
                    if not isinstance(rt, Mapping):
                        continue
                    norm = normalize_turn_tokens(rt)
                    fresh_inp = norm["fresh_tokens"]
                    cac = norm["cached_tokens"]
                    prompt_tok = norm["prompt_tokens"]
                    out = norm["output_tokens"]
                    thk = norm["thinking_tokens"]
                    model = str(
                        rt.get("model_name")
                        or rt.get("model")
                        or pricing.config.default_model
                    )
                    if (fresh_inp + cac + out + thk) > 0:
                        cost_info = self._calc_model_cost(
                            model,
                            fresh_inp,
                            cac,
                            out,
                            thk,
                            models_config=models_cfg,
                        )
                        cost = float(cost_info["total_cost"])
                        savings_dollars = float(cost_info["savings_dollars"])
                        uncached_cost = float(cost_info["uncached_cost"])
                    else:
                        cost = float(
                            rt.get("total_cost")
                            or rt.get("estimated_cost")
                            or rt.get("cost")
                            or 0.0
                        )
                        savings_dollars = 0.0
                        uncached_cost = cost
                    raw_tools = (
                        rt.get("tools_called")
                        if rt.get("tools_called") is not None
                        else rt.get("invoked_tools")
                    )
                    if isinstance(raw_tools, list):
                        tools_list = [
                            str(t).strip()
                            for t in raw_tools
                            if str(t).strip()
                            and str(t).strip() != "None (Direct Text)"
                        ]
                    elif isinstance(raw_tools, str) and raw_tools.strip():
                        tools_list = [
                            t
                            for t in _split_top_level_csv(raw_tools)
                            if t and t != "None (Direct Text)"
                        ]
                    else:
                        tools_list = []

                    raw_skills = (
                        rt.get("skills_active")
                        if rt.get("skills_active") is not None
                        else rt.get("invoked_skills")
                    )
                    if isinstance(raw_skills, list):
                        skills_list = [
                            str(s).strip()
                            for s in raw_skills
                            if str(s).strip()
                            and str(s).strip() not in ("None", "unload")
                        ]
                    elif isinstance(raw_skills, str) and raw_skills.strip():
                        skills_list = [
                            s
                            for s in _split_top_level_csv(raw_skills)
                            if s and s not in ("None", "unload")
                        ]
                    else:
                        skills_list = []

                    raw_json_val = rt.get("raw_json")
                    raw_meta: dict[str, Any] = (
                        raw_json_val if isinstance(raw_json_val, dict) else {}
                    )
                    sub_agent = str(
                        raw_meta.get("sub_agent")
                        or rt.get("sub_agent")
                        or rt.get("app_name")
                        or "local_agent"
                    )
                    orchestration_flow = str(
                        raw_meta.get("orchestration_flow")
                        or rt.get("orchestration_flow")
                        or ""
                    )
                    fname = os.path.basename(tf)
                    is_live_file = "live_adk_session" in fname.lower()
                    sid = str(rt.get("session_id") or "session_local_dev")
                    inv_tools_str = ", ".join(tools_list)
                    inv_skills_str = ", ".join(skills_list)

                    raw_q = str(rt.get("user_query") or "").strip()
                    cleaned_q = self._clean_prompt_preview(raw_q)
                    if (
                        cleaned_q
                        and cleaned_q != "Turn"
                        and not cleaned_q.startswith("For context:")
                    ):
                        active_prompt_by_session[sid] = cleaned_q
                        display_q = cleaned_q
                    elif sid in active_prompt_by_session:
                        display_q = active_prompt_by_session[sid]
                    else:
                        display_q = cleaned_q or "Turn"

                    raw_resp = str(rt.get("agent_response") or "").strip()
                    if (not raw_resp or raw_resp == "—") and inv_tools_str:
                        raw_resp = f"🛠️ Tool call: {inv_tools_str}"

                    traces.append(
                        {
                            "app_name": sub_agent,
                            "parent_app": str(
                                rt.get("app_name") or "local_agent"
                            ),
                            "orchestration_flow": orchestration_flow,
                            "prompt_tokens": prompt_tok,
                            "input_tokens": fresh_inp,
                            "cached_tokens": cac,
                            "output_tokens": out,
                            "thinking_tokens": thk,
                            "estimated_cost": cost,
                            "total_cost": cost,
                            "savings_dollars": savings_dollars,
                            "uncached_cost": uncached_cost,
                            "latency_ms": int(rt.get("latency_ms") or 0),
                            "timestamp": str(
                                rt.get("timestamp")
                                or time.strftime("%Y-%m-%dT%H:%M:%SZ")
                            ),
                            "session_id": sid,
                            "user_query": display_q,
                            "raw_user_query": raw_q,
                            "agent_response": raw_resp or "—",
                            "model_name": model,
                            "tools_called": tools_list,
                            "skills_active": skills_list,
                            "invoked_tools": inv_tools_str,
                            "invoked_skills": inv_skills_str,
                            "event_type": "LLM_RESPONSE",
                            "trace_file": fname,
                            "is_live_file": is_live_file,
                        }
                    )
            except Exception:
                pass
        return traces

    @staticmethod
    def _clean_prompt_preview(raw_query: str) -> str:
        txt = str(raw_query or "").strip()
        while txt.startswith("[") and "]" in txt:
            txt = txt.split("]", 1)[1].strip()
        return " ".join(txt.split())

    def _build_local_sessions_map(
        self, local_turns: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        sessions_map: dict[str, dict[str, Any]] = {}
        for t in local_turns:
            sid = str(t.get("session_id") or "")
            if not sid:
                continue
            ts = str(t.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ"))
            clean_q = self._clean_prompt_preview(str(t.get("user_query") or ""))
            if sid not in sessions_map:
                sessions_map[sid] = {
                    "session_id": sid,
                    "start_time": ts,
                    "last_active_time": ts,
                    "turn_count": 0,
                    "agent_name": str(
                        t.get("parent_app") or t.get("app_name") or "agent"
                    ),
                    "prompts": [],
                    "is_live_file": bool(t.get("is_live_file")),
                }
            entry = sessions_map[sid]
            entry["turn_count"] += 1
            entry["start_time"] = min(entry["start_time"], ts)
            entry["last_active_time"] = max(ts, entry["last_active_time"])
            if t.get("is_live_file"):
                entry["is_live_file"] = True
            if (
                clean_q
                and clean_q != "Turn"
                and not clean_q.startswith("For context:")
                and (not entry["prompts"] or entry["prompts"][-1] != clean_q)
            ):
                entry["prompts"].append(clean_q)

        for _sid, info in sessions_map.items():
            prompts = info.pop("prompts", [])
            if not prompts:
                preview = info["agent_name"]
            elif len(prompts) >= 2 and len(prompts[0]) <= 6:
                preview = f"{prompts[0]} → {prompts[1]}"
            else:
                preview = prompts[0]
            if len(preview) > 42:
                preview = preview[:39] + "…"
            info["first_query"] = preview
        return sessions_map

    def fetch_sessions(self) -> list[dict[str, Any]]:
        explicit_trace = getattr(self, "traces_path", None)
        local_turns = self._load_local_trace_turns()
        merged_sessions: dict[str, dict[str, Any]] = (
            self._build_local_sessions_map(local_turns) if local_turns else {}
        )

        if not explicit_trace and bool(getattr(self, "project_id", None)):
            try:
                from google.cloud import bigquery

                proj, ds, tbl, vw = self._validated_bq_identifiers()
                cache_key = ("sessions", proj, ds, tbl, vw)
                bq_session_rows = self._get_bq_cached(cache_key)
                if bq_session_rows is None:
                    client = bigquery.Client(project=proj)
                    bq_session_rows = []
                    for idx_c, candidate in enumerate(
                        dict.fromkeys(
                            [vw, tbl, "adk_agent_events", "agent_events"]
                        )
                    ):
                        if not candidate:
                            continue
                        safe_cand = validate_bq_identifier(
                            candidate, "table_id"
                        )
                        is_view_cand = candidate == vw and idx_c == 0
                        is_explicit_nondefault = (
                            is_view_cand and vw != "v_llm_response"
                        ) or (
                            not is_view_cand
                            and candidate == tbl
                            and tbl not in ("adk_agent_events", "agent_events")
                        )
                        query = f"""
                            SELECT
                                session_id,
                                MIN(timestamp) AS start_time,
                                MAX(timestamp) AS last_active_time,
                                COUNT(*) AS turn_count,
                                ANY_VALUE(agent) AS agent_name
                            FROM `{proj}.{ds}.{safe_cand}`
                            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
                              AND session_id IS NOT NULL
                            GROUP BY session_id
                            ORDER BY last_active_time DESC
                            LIMIT 50
                        """
                        try:
                            rows = list(client.query(query).result())
                            for row in rows:
                                sid = str(
                                    TokenomicsWebHandler._row_val(
                                        row, "session_id", ""
                                    )
                                    or ""
                                )
                                if not sid:
                                    continue
                                st = TokenomicsWebHandler._row_val(
                                    row, "start_time", None
                                )
                                lt = (
                                    TokenomicsWebHandler._row_val(
                                        row, "last_active_time", None
                                    )
                                    or st
                                )
                                st_str = (
                                    st.isoformat()
                                    if hasattr(st, "isoformat")
                                    else str(st or "")
                                )
                                lt_str = (
                                    lt.isoformat()
                                    if hasattr(lt, "isoformat")
                                    else str(lt or st_str)
                                )
                                tc = int(
                                    TokenomicsWebHandler._row_val(
                                        row, "turn_count", 1
                                    )
                                    or 1
                                )
                                ag = str(
                                    TokenomicsWebHandler._row_val(
                                        row, "agent_name", ""
                                    )
                                    or "agent"
                                )
                                bq_session_rows.append(
                                    {
                                        "session_id": sid,
                                        "start_time": st_str,
                                        "last_active_time": lt_str,
                                        "turn_count": tc,
                                        "agent_name": ag,
                                    }
                                )
                            if rows or is_explicit_nondefault:
                                break
                        except Exception as sess_err:
                            if not self._is_missing_bq_table_error(sess_err):
                                raise sess_err
                            continue
                    self._set_bq_cached(cache_key, bq_session_rows)

                has_local = bool(merged_sessions)
                for idx, s_row in enumerate(bq_session_rows):
                    sid = s_row["session_id"]
                    lt_str = s_row["last_active_time"]
                    tc = s_row["turn_count"]
                    ag = s_row["agent_name"]
                    if sid in merged_sessions:
                        existing = merged_sessions[sid]
                        if lt_str > str(existing.get("last_active_time") or ""):
                            existing["last_active_time"] = lt_str
                        existing["turn_count"] = max(
                            int(existing.get("turn_count") or 1), tc
                        )
                    else:
                        merged_sessions[sid] = {
                            "session_id": sid,
                            "start_time": s_row["start_time"],
                            "last_active_time": lt_str,
                            "turn_count": tc,
                            "agent_name": ag,
                            "first_query": ag,
                            "is_live_file": (idx == 0 and not has_local),
                        }
            except Exception:
                pass

        if merged_sessions:
            result_list = list(merged_sessions.values())
            result_list.sort(
                key=lambda s: (
                    1 if s.get("is_live_file") else 0,
                    str(s.get("last_active_time") or ""),
                ),
                reverse=True,
            )
            return result_list

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return [
            {
                "session_id": "session_local_dev",
                "start_time": now_iso,
                "last_active_time": now_iso,
                "turn_count": 0,
                "agent_name": "local_agent",
                "first_query": "Waiting for first turn…",
                "is_live_file": True,
            }
        ]

    def fetch_metrics(self, session_id: str = "global") -> dict[str, Any]:
        explicit_trace = getattr(self, "traces_path", None)
        resolved_session_id = session_id or "global"
        if resolved_session_id == "latest":
            sessions = self.fetch_sessions()
            resolved_session_id = (
                sessions[0]["session_id"] if sessions else "global"
            )

        local_turns = self._load_local_trace_turns()
        matched_local = (
            [
                t
                for t in local_turns
                if t.get("session_id") == resolved_session_id
            ]
            if (resolved_session_id and resolved_session_id != "global")
            else list(local_turns)
        )

        # 1. If --traces was explicitly provided on CLI, or the requested session is a live local trace file
        if explicit_trace or (
            resolved_session_id != "global"
            and matched_local
            and any(t.get("is_live_file") for t in matched_local)
        ):
            return self._build_metrics_response(
                matched_local,
                source="local_traces",
                active_session_id=resolved_session_id,
            )

        # 2. Try BigQuery when project_id is configured
        if getattr(self, "project_id", ""):
            try:
                from google.cloud import bigquery

                proj, ds, tbl, vw = self._validated_bq_identifiers()
                cache_key = ("metrics", proj, ds, tbl, vw, resolved_session_id)
                cached_resp = self._get_bq_cached(cache_key)
                if cached_resp is not None:
                    return cached_resp

                client = bigquery.Client(project=proj)
                where_parts = [
                    "timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
                ]
                query_params = []
                if resolved_session_id and resolved_session_id != "global":
                    where_parts.append("session_id = @session_id")
                    query_params.append(
                        bigquery.ScalarQueryParameter(
                            "session_id", "STRING", resolved_session_id
                        )
                    )
                where_clause = "WHERE " + " AND ".join(where_parts)
                job_config = (
                    bigquery.QueryJobConfig(query_parameters=query_params)
                    if query_params
                    else None
                )

                turn_limit = 500
                rows = None
                used_candidate = vw
                used_is_view = True
                used_view_has_tool_use = True
                for idx_c, candidate in enumerate(
                    dict.fromkeys([vw, tbl, "adk_agent_events", "agent_events"])
                ):
                    if not candidate:
                        continue
                    safe_cand = validate_bq_identifier(candidate, "table_id")
                    target = f"`{proj}.{ds}.{safe_cand}`"
                    is_view = candidate == vw and idx_c == 0
                    is_explicit_nondefault = (
                        is_view and vw != "v_llm_response"
                    ) or (
                        not is_view
                        and candidate == tbl
                        and tbl not in ("adk_agent_events", "agent_events")
                    )
                    if is_view:
                        cols = (
                            "timestamp, event_type, agent, session_id, invocation_id, "
                            "model_version, usage_prompt_tokens, usage_tool_use_tokens, usage_cached_tokens, "
                            "usage_completion_tokens, total_ms, user_query, agent_response, "
                            "attributes, content"
                        )
                    else:
                        cols = "timestamp, event_type, agent, session_id, invocation_id, content, attributes, latency_ms"
                    query = f"""
                        SELECT {cols}
                        FROM {target}
                        {where_clause}
                        ORDER BY timestamp DESC
                        LIMIT {turn_limit}
                    """
                    try:
                        rows = list(
                            client.query(query, job_config=job_config).result()
                        )
                        used_candidate = safe_cand
                        used_is_view = is_view
                        used_view_has_tool_use = True
                        if rows or is_explicit_nondefault:
                            break
                    except Exception as q_err:
                        if is_view and self._is_missing_tool_use_col_error(
                            q_err
                        ):
                            legacy_cols = (
                                "timestamp, event_type, agent, session_id, invocation_id, "
                                "model_version, usage_prompt_tokens, usage_cached_tokens, "
                                "usage_completion_tokens, total_ms, user_query, agent_response, "
                                "attributes, content"
                            )
                            legacy_q = f"""
                                SELECT {legacy_cols}
                                FROM {target}
                                {where_clause}
                                ORDER BY timestamp DESC
                                LIMIT {turn_limit}
                            """
                            try:
                                rows = list(
                                    client.query(
                                        legacy_q, job_config=job_config
                                    ).result()
                                )
                                used_candidate = safe_cand
                                used_is_view = True
                                used_view_has_tool_use = False
                                if rows or is_explicit_nondefault:
                                    break
                                continue
                            except Exception as leg_err:
                                q_err = leg_err
                        rows = None
                        if not self._is_missing_bq_table_error(q_err):
                            raise q_err
                        continue

                if rows is not None:
                    pricing = (
                        self._get_active_pricing_engine()
                        if hasattr(self, "_get_active_pricing_engine")
                        else (getattr(self, "pricing", None) or PricingEngine())
                    )
                    parsed_chronological = [
                        self._parse_bq_event_row(r, pricing=pricing)
                        for r in reversed(rows)
                    ]
                    turns = self._correlate_bq_events(
                        parsed_chronological, filter_llm_turns=True
                    )
                    if matched_local:
                        local_prompts = [
                            str(lt.get("user_query") or "")
                            for lt in matched_local
                            if lt.get("user_query")
                            and lt.get("user_query") != "Turn"
                        ]
                        for idx_t, bt in enumerate(turns):
                            if not bt.get("user_query") and idx_t < len(
                                local_prompts
                            ):
                                bt["user_query"] = local_prompts[idx_t]

                    agg_rows: list[Any] | None = None
                    if len(rows) >= turn_limit:
                        try:
                            agg_target = f"`{proj}.{ds}.{used_candidate}`"
                            if used_is_view:
                                view_agg_prompt = (
                                    "COALESCE(usage_prompt_tokens, 0) + COALESCE(usage_tool_use_tokens, 0)"
                                    if used_view_has_tool_use
                                    else (
                                        "COALESCE(usage_prompt_tokens, 0) + COALESCE("
                                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64), "
                                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64), "
                                        "SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64), 0)"
                                    )
                                )
                                agg_q = f"""
                                    SELECT
                                        COALESCE(JSON_VALUE(attributes, '$.sub_agent'), agent, 'agent') AS app_name,
                                        COALESCE(model_version, 'gemini-3.8-flash') AS model_name,
                                        ANY_VALUE(NULLIF(JSON_VALUE(attributes, '$.orchestration_flow'), '')) AS orchestration_flow,
                                        COUNT(*) AS turns,
                                        COALESCE(SUM({view_agg_prompt}), 0) AS prompt_tokens,
                                        COALESCE(SUM(usage_cached_tokens), 0) AS cached_tokens,
                                        COALESCE(SUM(usage_completion_tokens), 0) AS output_tokens,
                                        COALESCE(SUM(COALESCE(
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64),
                                            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64),
                                            0
                                        )), 0) AS thinking_tokens
                                    FROM {agg_target}
                                    {where_clause}
                                    GROUP BY app_name, model_name
                                """
                            else:
                                agg_prompt_expr = (
                                    "("
                                    "COALESCE("
                                    "SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64), "
                                    "0) + COALESCE("
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64), "
                                    "0)"
                                    ")"
                                )
                                agg_cached_expr = (
                                    "COALESCE("
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64), "
                                    "0)"
                                )
                                agg_out_expr = (
                                    "COALESCE("
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64), "
                                    "0)"
                                )
                                agg_think_expr = (
                                    "COALESCE("
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64), "
                                    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64), "
                                    "0)"
                                )
                                agg_q = f"""
                                    SELECT
                                        COALESCE(JSON_VALUE(attributes, '$.sub_agent'), agent, 'agent') AS app_name,
                                        COALESCE(JSON_VALUE(attributes, '$.model_version'), JSON_VALUE(attributes, '$.model'), JSON_VALUE(content, '$.model_version'), 'gemini-3.8-flash') AS model_name,
                                        ANY_VALUE(NULLIF(JSON_VALUE(attributes, '$.orchestration_flow'), '')) AS orchestration_flow,
                                        COUNT(*) AS turns,
                                        COALESCE(SUM({agg_prompt_expr}), 0) AS prompt_tokens,
                                        COALESCE(SUM({agg_cached_expr}), 0) AS cached_tokens,
                                        COALESCE(SUM({agg_out_expr}), 0) AS output_tokens,
                                        COALESCE(SUM({agg_think_expr}), 0) AS thinking_tokens
                                    FROM {agg_target}
                                    {where_clause} AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
                                    GROUP BY app_name, model_name
                                """
                            agg_rows = list(
                                client.query(
                                    agg_q, job_config=job_config
                                ).result()
                            )
                        except Exception:
                            agg_rows = None

                    if turns or not matched_local:
                        resp = self._build_metrics_response(
                            turns,
                            source="bigquery",
                            active_session_id=resolved_session_id,
                            server_agg_rows=agg_rows,
                        )
                        self._set_bq_cached(cache_key, resp)
                        return resp
            except Exception as e:
                print(
                    f"[WARN] BigQuery fetch failed: {e}. Falling back to local traces.",
                    flush=True,
                )

        # 3. Fallback to Local Traces
        if matched_local:
            return self._build_metrics_response(
                matched_local,
                source="local_traces",
                active_session_id=resolved_session_id,
            )

        return self._build_metrics_response(
            [],
            source="bigquery"
            if getattr(self, "project_id", "")
            else "local_traces",
            active_session_id=resolved_session_id,
        )

    @staticmethod
    def _infer_session_orchestration_flows(turns: list[dict[str, Any]]) -> None:
        """Infers ADK orchestration flow (Root, AgentTool, Parallel, Loop, Sequential, and compounds) for trace turns lacking runtime parent metadata."""
        if not turns:
            return

        by_session: dict[str, list[dict[str, Any]]] = {}
        for t in turns:
            sid = str(t.get("session_id") or "default")
            by_session.setdefault(sid, []).append(t)

        builtin_tool_names = {
            "transfer_to_agent",
            "exit_loop",
            "load_skill",
            "activate_skill",
            "unload_skill",
        }

        for sid_turns in by_session.values():
            distinct_agents = list(
                dict.fromkeys(
                    str(t.get("app_name") or "agent") for t in sid_turns
                )
            )
            if len(distinct_agents) <= 1:
                for t in sid_turns:
                    if not t.get("orchestration_flow"):
                        t["orchestration_flow"] = "Root"
                continue

            root_agent = distinct_agents[0]
            for t in sid_turns:
                tools_str = str(t.get("invoked_tools") or "")
                if "transfer_to_agent" in tools_str:
                    root_agent = str(t.get("app_name") or root_agent)
                    break

            explicit_flow_by_agent: dict[str, str] = {}
            invoked_tool_fn_names: set[str] = set()
            for t in sid_turns:
                ag_t = str(t.get("app_name") or "agent")
                f_t = str(t.get("orchestration_flow") or "").strip()
                if f_t and f_t != "Sub-Agent":
                    prev_f = explicit_flow_by_agent.get(ag_t, "")
                    if not prev_f or len(f_t) > len(prev_f):
                        explicit_flow_by_agent[ag_t] = f_t

                raw_t_items = (
                    list(t["tools_called"])
                    if isinstance(t.get("tools_called"), list)
                    and t["tools_called"]
                    else _split_top_level_csv(str(t.get("invoked_tools") or ""))
                )
                for raw_item in raw_t_items:
                    fn_base = str(raw_item).split("(", 1)[0].strip()
                    if (
                        fn_base
                        and fn_base not in builtin_tool_names
                        and fn_base != ag_t
                    ):
                        invoked_tool_fn_names.add(fn_base)

            agent_tool_agents = {
                ag
                for ag in distinct_agents
                if ag != root_agent and ag in invoked_tool_fn_names
            }

            exit_loop_idx = -1
            for idx, t in enumerate(sid_turns):
                if "exit_loop" in str(t.get("invoked_tools") or ""):
                    exit_loop_idx = idx

            loop_agents = set()
            if exit_loop_idx >= 0:
                for idx in range(exit_loop_idx + 1):
                    ag = str(sid_turns[idx].get("app_name") or "agent")
                    if ag != root_agent:
                        loop_agents.add(ag)

            parallel_agents = set()
            seq_agents = [str(t.get("app_name") or "agent") for t in sid_turns]
            for i in range(len(seq_agents) - 2):
                a1, a2, a3 = seq_agents[i], seq_agents[i + 1], seq_agents[i + 2]
                if a1 == a3 and a1 not in (a2, root_agent) and a2 != root_agent:
                    parallel_agents.add(a1)
                    parallel_agents.add(a2)

            for t in sid_turns:
                cur_flow = str(t.get("orchestration_flow") or "").strip()
                if cur_flow and cur_flow != "Sub-Agent":
                    continue
                ag = str(t.get("app_name") or "agent")
                if ag in explicit_flow_by_agent:
                    t["orchestration_flow"] = explicit_flow_by_agent[ag]
                elif ag == root_agent:
                    t["orchestration_flow"] = "Root"
                elif ag in agent_tool_agents:
                    if ag in parallel_agents and ag in loop_agents:
                        t["orchestration_flow"] = "AgentTool • Parallel • Loop"
                    elif ag in parallel_agents:
                        t["orchestration_flow"] = "AgentTool • Parallel"
                    elif ag in loop_agents:
                        t["orchestration_flow"] = "AgentTool • Loop"
                    else:
                        t["orchestration_flow"] = "AgentTool"
                elif ag in parallel_agents and ag in loop_agents:
                    t["orchestration_flow"] = "Parallel • Loop"
                elif ag in parallel_agents:
                    t["orchestration_flow"] = "Parallel"
                elif ag in loop_agents:
                    t["orchestration_flow"] = "Loop"
                else:
                    t["orchestration_flow"] = "Sequential"

    def _build_metrics_response(
        self,
        turns: list[dict[str, Any]],
        source: str | None = None,
        active_session_id: str | None = None,
        server_agg_rows: list[Any] | None = None,
    ) -> dict[str, Any]:
        self._infer_session_orchestration_flows(turns)
        active_models_cfg = self.load_models_config()

        total_p = sum(int(t.get("prompt_tokens", 0)) for t in turns)
        total_c = sum(int(t.get("cached_tokens", 0)) for t in turns)
        total_o = sum(int(t.get("output_tokens", 0)) for t in turns)
        total_th = sum(int(t.get("thinking_tokens", 0)) for t in turns)
        total_cost = sum(float(t.get("estimated_cost", 0.0)) for t in turns)
        total_savings = 0.0

        metrics_by_app: dict[str, dict[str, Any]] = {}
        tool_counts: dict[str, dict[str, Any]] = {}

        for t in turns:
            app_key = str(t.get("app_name") or "agent")
            flow_lbl = str(t.get("orchestration_flow") or "")
            if "savings_dollars" in t:
                t_savings = float(t.get("savings_dollars") or 0.0)
            else:
                p_tok = int(t.get("prompt_tokens", 0))
                c_tok = int(t.get("cached_tokens", 0))
                f_tok = (
                    int(t.get("input_tokens") or 0)
                    if "input_tokens" in t
                    else max(0, p_tok - c_tok)
                )
                o_tok = int(t.get("output_tokens", 0))
                th_tok = int(t.get("thinking_tokens", 0))
                m_id = str(t.get("model_name") or "gemini-3.8-flash")
                c_info = self._calc_model_cost(
                    m_id,
                    f_tok,
                    c_tok,
                    o_tok,
                    th_tok,
                    models_config=active_models_cfg,
                )
                t_savings = float(c_info["savings_dollars"])
                t["savings_dollars"] = t_savings
                t["uncached_cost"] = (
                    float(t.get("estimated_cost", 0.0)) + t_savings
                )

            total_savings += t_savings

            turn_model_raw = str(t.get("model_name") or "").strip()
            if app_key not in metrics_by_app:
                metrics_by_app[app_key] = {
                    "name": app_key,
                    "orchestration_flow": flow_lbl,
                    "model_name": turn_model_raw,
                    "models": [],
                    "turns": 0,
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "thinking": 0,
                    "cost": 0.0,
                    "savings_dollars": 0.0,
                    "cost_share_pct": 0.0,
                }
            entry = metrics_by_app[app_key]
            if turn_model_raw:
                entry["model_name"] = turn_model_raw
                if turn_model_raw not in entry["models"]:
                    entry["models"].append(turn_model_raw)
            cur_entry_flow = str(entry.get("orchestration_flow") or "")
            if flow_lbl and (
                not cur_entry_flow
                or cur_entry_flow == "Sub-Agent"
                or len(flow_lbl) > len(cur_entry_flow)
            ):
                entry["orchestration_flow"] = flow_lbl
            entry["turns"] += 1
            entry["input"] += int(t.get("prompt_tokens", 0))
            entry["cached"] += int(t.get("cached_tokens", 0))
            entry["output"] += int(t.get("output_tokens", 0))
            entry["thinking"] += int(t.get("thinking_tokens", 0))
            entry["cost"] += float(t.get("estimated_cost", 0.0))
            entry["savings_dollars"] += t_savings

            turn_tools = (
                [
                    str(x).strip()
                    for x in t["tools_called"]
                    if str(x).strip() and str(x).strip() != "None (Direct Text)"
                ]
                if isinstance(t.get("tools_called"), list)
                else [
                    x
                    for x in _split_top_level_csv(
                        str(t.get("invoked_tools") or "")
                    )
                    if x and x != "None (Direct Text)"
                ]
            )
            for t_name in turn_tools:
                if t_name not in tool_counts:
                    tool_counts[t_name] = {
                        "name": t_name,
                        "kind": "tool",
                        "label": f"Sub-Agent: {app_key}",
                        "icon": "🛠️",
                        "count": 0,
                        "targets": {},
                    }
                tool_counts[t_name]["count"] += 1
                tool_counts[t_name]["targets"][app_key] = (
                    tool_counts[t_name]["targets"].get(app_key, 0) + 1
                )

            turn_skills = (
                [
                    str(x).strip()
                    for x in t["skills_active"]
                    if str(x).strip()
                    and str(x).strip() not in ("None", "unload")
                ]
                if isinstance(t.get("skills_active"), list)
                else [
                    x
                    for x in _split_top_level_csv(
                        str(t.get("invoked_skills") or "")
                    )
                    if x and x not in ("None", "unload")
                ]
            )
            for s_name in turn_skills:
                sk_key = (
                    f"skill:{s_name}"
                    if (
                        s_name in tool_counts
                        and tool_counts[s_name].get("kind") != "skill"
                    )
                    else s_name
                )
                if sk_key not in tool_counts:
                    tool_counts[sk_key] = {
                        "name": s_name,
                        "kind": "skill",
                        "label": f"Activated Skill ({app_key})",
                        "icon": "⚡",
                        "count": 0,
                        "targets": {},
                    }
                tool_counts[sk_key]["count"] += 1
                tool_counts[sk_key]["targets"][app_key] = (
                    tool_counts[sk_key]["targets"].get(app_key, 0) + 1
                )

        total_turns_count = len(turns)
        if server_agg_rows:
            agg_by_app: dict[str, dict[str, Any]] = {}
            total_p = 0
            total_c = 0
            total_o = 0
            total_th = 0
            total_cost = 0.0
            total_savings = 0.0
            total_turns_count = 0
            for ar in server_agg_rows:
                a_key = str(
                    TokenomicsWebHandler._row_val(ar, "app_name", "agent")
                    or "agent"
                )
                m_key = str(
                    TokenomicsWebHandler._row_val(
                        ar, "model_name", "gemini-3.8-flash"
                    )
                    or "gemini-3.8-flash"
                )
                ar_flow = str(
                    TokenomicsWebHandler._row_val(ar, "orchestration_flow", "")
                    or ""
                )
                r_t = _safe_nonneg_int(
                    TokenomicsWebHandler._row_val(ar, "turns", 0) or 0
                )
                r_p = _safe_nonneg_int(
                    TokenomicsWebHandler._row_val(ar, "prompt_tokens", 0) or 0
                )
                r_c = _safe_nonneg_int(
                    TokenomicsWebHandler._row_val(ar, "cached_tokens", 0) or 0
                )
                r_o = _safe_nonneg_int(
                    TokenomicsWebHandler._row_val(ar, "output_tokens", 0) or 0
                )
                r_th = _safe_nonneg_int(
                    TokenomicsWebHandler._row_val(ar, "thinking_tokens", 0) or 0
                )
                r_fresh = max(0, r_p - r_c)
                c_info = self._calc_model_cost(
                    m_key,
                    r_fresh,
                    r_c,
                    r_o,
                    r_th,
                    models_config=active_models_cfg,
                )
                r_cost = float(c_info["total_cost"])
                r_sav = float(c_info["savings_dollars"])

                prev_flow = ar_flow or metrics_by_app.get(a_key, {}).get(
                    "orchestration_flow", ""
                )
                ae = agg_by_app.setdefault(
                    a_key,
                    {
                        "name": a_key,
                        "orchestration_flow": prev_flow,
                        "model_name": m_key,
                        "models": [],
                        "turns": 0,
                        "input": 0,
                        "cached": 0,
                        "output": 0,
                        "thinking": 0,
                        "cost": 0.0,
                        "savings_dollars": 0.0,
                        "cost_share_pct": 0.0,
                    },
                )
                if m_key:
                    ae["model_name"] = m_key
                    if m_key not in ae["models"]:
                        ae["models"].append(m_key)
                if prev_flow and not ae.get("orchestration_flow"):
                    ae["orchestration_flow"] = prev_flow
                ae["turns"] += r_t
                ae["input"] += r_p
                ae["cached"] += r_c
                ae["output"] += r_o
                ae["thinking"] += r_th
                ae["cost"] += r_cost
                ae["savings_dollars"] += r_sav

                total_turns_count += r_t
                total_p += r_p
                total_c += r_c
                total_o += r_o
                total_th += r_th
                total_cost += r_cost
                total_savings += r_sav
            if agg_by_app:
                metrics_by_app = agg_by_app

        for app_entry in metrics_by_app.values():
            if isinstance(app_entry, dict):
                app_cost = float(app_entry.get("cost") or 0.0)
                app_entry["cost_share_pct"] = round(
                    (app_cost / total_cost * 100.0) if total_cost > 0 else 0.0,
                    2,
                )

        turns_truncated = bool(
            total_turns_count > len(turns)
            or any(
                _safe_nonneg_int(m.get("turns", 0))
                > sum(
                    1 for t in turns if str(t.get("app_name") or "agent") == k
                )
                for k, m in metrics_by_app.items()
                if isinstance(m, dict)
            )
        )

        simulations = {}
        dynamic_models = (
            active_models_cfg.get("models")
            if isinstance(active_models_cfg, dict)
            else None
        )
        fresh_total = max(0, total_p - total_c)
        if isinstance(dynamic_models, list) and dynamic_models:
            for m in dynamic_models:
                if not isinstance(m, dict):
                    continue
                m_name = str(m.get("name") or m.get("id") or "Model")
                p_cfg = m.get("pricing") or {}
                in_rate_per_m = float(
                    p_cfg.get("input", p_cfg.get("input_per_million", 0.75))
                )
                cac_rate_per_m = float(
                    p_cfg.get(
                        "cached",
                        p_cfg.get("cached_per_million", in_rate_per_m * 0.25),
                    )
                )
                out_rate_per_m = float(
                    p_cfg.get("output", p_cfg.get("output_per_million", 3.75))
                )
                in_rate = in_rate_per_m / 1_000_000.0
                cac_rate = cac_rate_per_m / 1_000_000.0
                out_rate = out_rate_per_m / 1_000_000.0
                simulations[m_name] = (
                    (fresh_total * in_rate)
                    + (total_c * cac_rate)
                    + ((total_o + total_th) * out_rate)
                )
        else:
            for m_name, rates in PRICING_MODELS.items():
                simulations[m_name] = (
                    (fresh_total * rates["prompt"])
                    + (total_c * rates["cached"])
                    + ((total_o + total_th) * rates["output"])
                )

        explicit_trace = getattr(self, "traces_path", None)
        resolved_source = source or (
            "local_traces" if explicit_trace else "bigquery"
        )
        return {
            "source": resolved_source,
            "active_session_id": str(active_session_id or "global"),
            "traces_path": str(explicit_trace or "artifacts/traces/"),
            "metrics": metrics_by_app,
            "total_turns": total_turns_count,
            "turns_truncated": turns_truncated,
            "total_cost": total_cost,
            "savings_dollars": total_savings,
            "uncached_cost": total_cost + total_savings,
            "total_tokens": total_p + total_o + total_th,
            "prompt_tokens": total_p,
            "cached_tokens": total_c,
            "output_tokens": total_o,
            "thinking_tokens": total_th,
            "turns": turns,
            "simulations": simulations,
            "tool_counts": tool_counts,
        }


def run_web_server(
    port: int = 8080,
    host: str = "127.0.0.1",
    project_id: str | None = None,
    dataset_id: str | None = None,
    table_id: str | None = None,
    view_id: str | None = None,
    traces_path: str | None = None,
    open_browser: bool = True,
    dev_reload: bool = False,
    allowed_hosts: Sequence[str] | None = None,
):
    """Starts the ADK Token Control Tower embedded web server."""
    if project_id is None and not traces_path:
        project_id = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("BIGQUERY_PROJECT_ID")
            or ""
        )
    project_id = str(project_id or "").strip()
    if project_id:
        project_id = validate_bq_project_id(project_id)
    env_ds, env_tbl, env_vw = resolve_env_bq_identifiers()
    resolved_ds = env_ds if dataset_id is None else str(dataset_id)
    resolved_tbl = env_tbl if table_id is None else str(table_id)
    resolved_vw = env_vw if view_id is None else str(view_id)
    dataset_id = validate_bq_identifier(resolved_ds, "dataset_id")
    table_id = validate_bq_identifier(resolved_tbl, "table_id")
    view_id = validate_bq_identifier(resolved_vw, "view_id")
    validated_allowed_hosts = validate_allowed_hosts(allowed_hosts)

    TokenomicsWebHandler.set_server_context(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        view_id=view_id,
        traces_path=traces_path,
        allowed_hosts=validated_allowed_hosts,
        bound_host=host,
    )

    raw_dev_reload = os.environ.get("TOKENOMICS_DEV_RELOAD")
    enable_hot_reload = bool(
        dev_reload
        or (raw_dev_reload is not None and raw_dev_reload.strip() == "1")
    )
    if enable_hot_reload:
        import importlib

        _state = {
            "mtime": os.path.getmtime(__file__)
            if os.path.exists(__file__)
            else 0.0
        }

        def _handler_factory(*args, **kwargs):
            try:
                cur_mtime = (
                    os.path.getmtime(__file__)
                    if os.path.exists(__file__)
                    else 0.0
                )
                if cur_mtime > _state["mtime"]:
                    _state["mtime"] = cur_mtime
                    mod = importlib.reload(sys.modules[__name__])
                    mod.TokenomicsWebHandler.set_server_context(
                        project_id=project_id,
                        dataset_id=dataset_id,
                        table_id=table_id,
                        view_id=view_id,
                        traces_path=traces_path,
                        allowed_hosts=validated_allowed_hosts,
                        bound_host=host,
                    )
                    return mod.TokenomicsWebHandler(*args, **kwargs)
            except Exception:
                pass
            return TokenomicsWebHandler(*args, **kwargs)

        handler_cls: Any = _handler_factory
    else:
        handler_cls = TokenomicsWebHandler

    server_address = (host, port)
    httpd = http.server.ThreadingHTTPServer(server_address, handler_cls)

    url = f"http://{host}:{port}"
    print("\n" + "=" * 80)
    print(" ⚡ ADK TOKEN CONTROL TOWER - WEB OBSERVATORY")
    print("=" * 80)
    print(f" • Dashboard URL : {url}")
    print(f" • Rate Card     : {get_models_config_path()}")
    if project_id:
        print(f" • BigQuery Host : {project_id}.{dataset_id}.{view_id}")
    if traces_path:
        print(f" • Local Traces  : {traces_path}")
    print("=" * 80)
    print("🚀 Server running. Press Ctrl+C to stop.\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[SYS] Web Observatory shutting down cleanly.")
        sys.exit(0)


# Backward-compatible handler alias
AgentNexusHandler = TokenomicsWebHandler
