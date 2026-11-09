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

# ruff: noqa: B027, S110, S608
"""
Tokenomics SDK - Pluggable Storage & Telemetry Sinks
Supports In-Memory and Production BigQuery streaming.
"""

import abc
import atexit
import copy
import datetime
import json
import math
import queue
import threading
import time
from collections.abc import Mapping
from typing import Any

from adk_tokenomics_plugin.core.config import (
    TokenomicsConfig,
    validate_bq_identifier,
    validate_bq_project_id,
)
from adk_tokenomics_plugin.core.pricing import PricingEngine, _safe_nonneg_int

_MAX_BQ_CHUNK_SIZE = 500
_ADK_ONLY_COLUMNS = (
    "event_type",
    "attributes",
    "content",
    "invocation_id",
    "latency_ms",
)


def _first_not_none(*values: Any) -> Any:
    for val in values:
        if val is not None:
            return val
    return None


def _first_non_empty_str(*values: Any) -> str:
    for val in values:
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    return ""


def _parse_json_mapping(raw_val: Any) -> dict[str, Any]:
    if isinstance(raw_val, Mapping):
        return dict(raw_val)
    if isinstance(raw_val, str):
        stripped = raw_val.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, Mapping):
                    return dict(parsed)
            except Exception:
                return {}
    return {}


def _safe_nonneg_float(val: Any) -> float:
    if val is None or isinstance(val, bool):
        return 0.0
    try:
        parsed = float(val)
        if not math.isfinite(parsed):
            return 0.0
        return max(0.0, parsed)
    except (ValueError, TypeError):
        return 0.0


def _extract_latency_ms_value(raw_lat: Any) -> int:
    if raw_lat is None or isinstance(raw_lat, bool):
        return 0
    if isinstance(raw_lat, (int, float)):
        return _safe_nonneg_int(raw_lat) if math.isfinite(float(raw_lat)) else 0
    if isinstance(raw_lat, Mapping):
        return _safe_nonneg_int(raw_lat.get("total_ms"))
    if isinstance(raw_lat, str):
        s_lat = raw_lat.strip()
        if not s_lat:
            return 0
        if s_lat.startswith("{"):
            parsed = _parse_json_mapping(s_lat)
            return _safe_nonneg_int(parsed.get("total_ms"))
        try:
            f_lat = float(s_lat)
            return max(0, round(f_lat)) if math.isfinite(f_lat) else 0
        except (ValueError, TypeError):
            return 0
    return 0


def parse_bq_turn_record(
    row: Mapping[str, Any],
    default_model: str = "publishers/google/models/gemini-3.8-flash",
    pricing_engine: PricingEngine | None = None,
) -> dict[str, Any]:
    """Normalizes ADK view rows (upstream, v1.0.2, pre-v1.0.2), raw adk_agent_events rows, or legacy flat rows into a canonical turn record."""
    if not isinstance(row, Mapping):
        return {}

    attr_dict = _parse_json_mapping(row.get("attributes"))
    content_dict = _parse_json_mapping(row.get("content"))
    raw_meta = _parse_json_mapping(row.get("raw_json"))
    usage_meta = (
        dict(attr_dict["usage_metadata"])
        if isinstance(attr_dict.get("usage_metadata"), Mapping)
        else {}
    )
    sess_meta = (
        dict(attr_dict["session_metadata"])
        if isinstance(attr_dict.get("session_metadata"), Mapping)
        else {}
    )
    content_usage = (
        dict(content_dict["usage"])
        if isinstance(content_dict.get("usage"), Mapping)
        else {}
    )

    raw_ts = row.get("timestamp")
    if isinstance(raw_ts, datetime.datetime):
        timestamp_str = raw_ts.isoformat()
    else:
        timestamp_str = str(raw_ts or "")

    raw_ev_type = row.get("event_type")
    event_type = (
        str(raw_ev_type).strip()
        if (raw_ev_type is not None and str(raw_ev_type).strip())
        else "LLM_RESPONSE"
    )

    session_id = (
        _first_non_empty_str(row.get("session_id"), sess_meta.get("session_id"))
        or "default_session"
    )
    invocation_id = _first_non_empty_str(
        row.get("invocation_id"),
        attr_dict.get("invocation_id"),
        raw_meta.get("invocation_id"),
    )

    app_name = (
        _first_non_empty_str(
            row.get("app_name"),
            sess_meta.get("app_name"),
            row.get("agent"),
            attr_dict.get("sub_agent"),
        )
        or "agent_app"
    )
    sub_agent = (
        _first_non_empty_str(
            row.get("sub_agent"),
            attr_dict.get("sub_agent"),
            raw_meta.get("sub_agent"),
            row.get("agent"),
        )
        or app_name
    )
    agent_col = _first_non_empty_str(row.get("agent"), sub_agent) or app_name
    orchestration_flow = _first_non_empty_str(
        row.get("orchestration_flow"),
        attr_dict.get("orchestration_flow"),
        raw_meta.get("orchestration_flow"),
    )

    model_name = _first_non_empty_str(
        row.get("model_name"),
        row.get("model_version"),
        attr_dict.get("model_version"),
        attr_dict.get("model"),
        content_dict.get("model_version"),
    ) or str(default_model or "publishers/google/models/gemini-3.8-flash")

    raw_tool_use = _first_not_none(
        row.get("usage_tool_use_tokens"),
        usage_meta.get("tool_use_prompt_token_count"),
        usage_meta.get("toolUsePromptTokenCount"),
        attr_dict.get("usage_tool_use_tokens"),
    )
    tool_use_tokens = (
        _safe_nonneg_int(raw_tool_use) if raw_tool_use is not None else 0
    )

    raw_cached = _first_not_none(
        row.get("cached_tokens"),
        row.get("usage_cached_tokens"),
        usage_meta.get("cached_content_token_count"),
        usage_meta.get("cachedContentTokenCount"),
        usage_meta.get("cacheReadTokens"),
        attr_dict.get("usage_cached_tokens"),
    )
    cached_tokens = (
        _safe_nonneg_int(raw_cached) if raw_cached is not None else 0
    )

    if (
        row.get("input_tokens") is not None
        and row.get("usage_prompt_tokens") is None
    ):
        fresh_input = _safe_nonneg_int(row.get("input_tokens"))
        prompt_tokens = fresh_input + cached_tokens
    else:
        if row.get("usage_prompt_tokens") is not None:
            prompt_tokens = (
                _safe_nonneg_int(row.get("usage_prompt_tokens"))
                + tool_use_tokens
            )
        elif row.get("prompt_tokens") is not None:
            prompt_tokens = (
                _safe_nonneg_int(row.get("prompt_tokens")) + tool_use_tokens
            )
        else:
            raw_base_prompt = _first_not_none(
                content_usage.get("prompt"),
                usage_meta.get("prompt_token_count"),
                usage_meta.get("promptTokens"),
            )
            if raw_base_prompt is not None:
                prompt_tokens = (
                    _safe_nonneg_int(raw_base_prompt) + tool_use_tokens
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
        fresh_input = max(0, prompt_tokens - cached_tokens)

    raw_out = _first_not_none(
        row.get("output_tokens"),
        row.get("completion_tokens"),
        row.get("usage_completion_tokens"),
        content_usage.get("completion"),
        usage_meta.get("candidates_token_count"),
        usage_meta.get("candidatesTokenCount"),
        attr_dict.get("usage_completion_tokens"),
    )
    output_tokens = _safe_nonneg_int(raw_out) if raw_out is not None else 0

    raw_thk = _first_not_none(
        row.get("thinking_tokens"),
        row.get("usage_thinking_tokens"),
        usage_meta.get("thoughts_token_count"),
        usage_meta.get("thoughtsTokenCount"),
        usage_meta.get("thinkingOutputTokens"),
        attr_dict.get("usage_thinking_tokens"),
    )
    thinking_tokens = _safe_nonneg_int(raw_thk) if raw_thk is not None else 0
    total_tokens = prompt_tokens + output_tokens + thinking_tokens

    raw_lat = _first_not_none(
        row.get("total_ms"), row.get("latency_ms"), raw_meta.get("latency_ms")
    )
    latency_ms = _extract_latency_ms_value(raw_lat)

    user_query = _first_non_empty_str(
        row.get("user_query"),
        content_dict.get("user_query"),
        content_dict.get("prompt"),
    )
    if not user_query and isinstance(content_dict.get("parts"), list):
        part_texts = [
            str(p.get("text", ""))
            for p in content_dict["parts"]
            if isinstance(p, Mapping) and p.get("text")
        ]
        if part_texts and str(content_dict.get("role") or "").lower() == "user":
            user_query = " ".join(part_texts).strip()

    raw_resp = _first_not_none(
        row.get("agent_response"),
        row.get("response"),
        content_dict.get("response"),
        content_dict.get("result"),
    )
    if isinstance(raw_resp, Mapping) and isinstance(
        raw_resp.get("parts"), list
    ):
        agent_response = "".join(
            str(p.get("text", ""))
            for p in raw_resp["parts"]
            if isinstance(p, Mapping)
        ) or json.dumps(dict(raw_resp))
    elif isinstance(raw_resp, (Mapping, list)):
        try:
            agent_response = json.dumps(raw_resp)
        except Exception:
            agent_response = str(raw_resp)
    else:
        agent_response = str(raw_resp or "")

    tools_called: list[str] = []
    for candidate_tools in (
        row.get("tools_called"),
        attr_dict.get("tools_called"),
        content_dict.get("tool"),
    ):
        if isinstance(candidate_tools, (list, tuple, set)):
            for t_item in candidate_tools:
                t_str = str(t_item).strip()
                if t_str and t_str not in tools_called:
                    tools_called.append(t_str)
        elif isinstance(candidate_tools, str) and candidate_tools.strip():
            t_str = candidate_tools.strip()
            if t_str not in tools_called:
                tools_called.append(t_str)

    skills_active: list[str] = []
    for candidate_skills in (
        row.get("skills_active"),
        attr_dict.get("skills_active"),
    ):
        if isinstance(candidate_skills, (list, tuple, set)):
            for s_item in candidate_skills:
                s_str = str(s_item).strip()
                if s_str and s_str not in skills_active:
                    skills_active.append(s_str)
        elif isinstance(candidate_skills, str) and candidate_skills.strip():
            s_str = candidate_skills.strip()
            if s_str not in skills_active:
                skills_active.append(s_str)

    raw_cost = _first_not_none(
        row.get("total_cost"),
        row.get("estimated_cost"),
        attr_dict.get("total_cost"),
    )
    if raw_cost is not None:
        total_cost = _safe_nonneg_float(raw_cost)
    elif total_tokens > 0:
        pe = pricing_engine or PricingEngine()
        total_cost = float(
            pe.calculate_cost(
                model_name,
                fresh_input,
                cached_tokens,
                output_tokens,
                thinking_tokens,
            )
        )
    else:
        total_cost = 0.0

    return {
        "timestamp": timestamp_str,
        "event_type": event_type,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "app_name": app_name,
        "sub_agent": sub_agent,
        "agent": agent_col,
        "orchestration_flow": orchestration_flow,
        "model_name": model_name,
        "model_version": model_name,
        "user_query": user_query,
        "agent_response": agent_response,
        "prompt_tokens": prompt_tokens,
        "input_tokens": fresh_input,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "usage_tool_use_tokens": tool_use_tokens,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "latency_ms": latency_ms,
        "tools_called": tools_called,
        "skills_active": skills_active,
    }


class BaseSink(abc.ABC):
    @abc.abstractmethod
    def write_turn(self, record: dict[str, Any]):
        """Writes a single turn record to the sink."""
        pass

    def write(self, record: dict[str, Any]):
        """Alias for write_turn."""
        return self.write_turn(record)

    @abc.abstractmethod
    def fetch_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetches recent turn records."""
        pass

    def flush(self) -> None:
        """Flushes buffered turn records without closing the sink."""
        pass

    def close(self):
        """Clean up sink resources."""
        pass


class InMemorySink(BaseSink):
    """In-memory telemetry sink for testing and local prototyping."""

    def __init__(self):
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def write_turn(self, record: dict[str, Any]):
        with self._lock:
            self.records.append(record)

    def fetch_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self.records[-limit:]))

    def clear(self):
        with self._lock:
            self.records.clear()


class BigQuerySink(BaseSink):
    """Production BigQuery streaming sink with background queue, chunking, and retry backoff."""

    def __init__(
        self,
        config: TokenomicsConfig,
        max_retries: int = 3,
        retry_base_delay: float = 0.05,
    ):
        self.config = config
        self.project_id = (
            validate_bq_project_id(config.project_id)
            if config.project_id is not None
            else None
        )
        self.dataset_id = validate_bq_identifier(
            config.dataset_id if config.dataset_id is not None else "bq_adk_ds",
            "dataset_id",
        )
        self.table_id = validate_bq_identifier(
            config.table_id
            if config.table_id is not None
            else "adk_agent_events",
            "table_id",
        )
        self._client = None
        self._max_retries = max(0, int(max_retries))
        self._retry_base_delay = max(0.0, float(retry_base_delay))
        self._max_chunk_size = _MAX_BQ_CHUNK_SIZE
        self._flush_interval_seconds = max(
            0.05, float(config.flush_interval_seconds or 5.0)
        )
        self.dropped_rows_count = 0
        self._stats_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._table_format_cache: dict[str, str] = {}
        self._closed = False
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10000)
        self._stop_event = threading.Event()

        # Start background worker thread
        self._worker = threading.Thread(target=self._flush_loop, daemon=True)
        self._worker.start()
        atexit.register(self.close)

    def _get_client(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project_id)
        return self._client

    def _resolve_project_id(self, client: Any) -> str:
        raw_proj = self.project_id or getattr(client, "project", None)
        if not raw_proj:
            raise ValueError(
                "BigQuery project_id is not configured and could not be resolved from client."
            )
        return validate_bq_project_id(str(raw_proj))

    @staticmethod
    def _snapshot_record(record: dict[str, Any]) -> dict[str, Any]:
        """Deep-copies mutable turn record containers before queueing to prevent races with late skill attribution."""
        if not isinstance(record, dict):
            return record
        try:
            return copy.deepcopy(record)
        except Exception:
            snap = dict(record)
            if isinstance(snap.get("tools_called"), (list, tuple, set)):
                snap["tools_called"] = list(snap["tools_called"])
            if isinstance(snap.get("skills_active"), (list, tuple, set)):
                snap["skills_active"] = list(snap["skills_active"])
            if isinstance(snap.get("cost_breakdown"), dict):
                cb = dict(snap["cost_breakdown"])
                if isinstance(cb.get("rates_used"), dict):
                    cb["rates_used"] = dict(cb["rates_used"])
                snap["cost_breakdown"] = cb
            raw_j = snap.get("raw_json")
            if isinstance(raw_j, dict):
                try:
                    snap["raw_json"] = copy.deepcopy(raw_j)
                except Exception:
                    snap["raw_json"] = {
                        k: (
                            list(v)
                            if isinstance(v, list)
                            else (dict(v) if isinstance(v, dict) else v)
                        )
                        for k, v in raw_j.items()
                    }
            elif isinstance(raw_j, list):
                snap["raw_json"] = list(raw_j)
            return snap

    def write_turn(self, record: dict[str, Any]):
        with self._stats_lock:
            if self._closed:
                self.dropped_rows_count += 1
                return
        record_copy = self._snapshot_record(record)
        try:
            self._queue.put_nowait(record_copy)
        except queue.Full:
            with self._stats_lock:
                self.dropped_rows_count += 1
            print(
                "[WARN] Tokenomics BigQuerySink queue is full. Dropping turn record.",
                flush=True,
            )

    @staticmethod
    def _extract_schema_field_names(table_obj: Any) -> set[str]:
        schema_seq = getattr(table_obj, "schema", None)
        if not schema_seq:
            return set()
        names = {
            str(
                f.get("name")
                if isinstance(f, Mapping)
                else getattr(f, "name", "")
            )
            .strip()
            .lower()
            for f in schema_seq
        }
        names.discard("")
        return names

    def _detect_row_format(self, client: Any, table_ref: str) -> str:
        if bool(getattr(self.config, "legacy_flat_schema", False)):
            return "flat"
        with self._schema_lock:
            cached = self._table_format_cache.get(table_ref)
        if cached is not None:
            return cached

        get_table_fn = getattr(client, "get_table", None)
        if callable(get_table_fn):
            try:
                tbl_obj = get_table_fn(table_ref)
                field_names = self._extract_schema_field_names(tbl_obj)
                if field_names:
                    detected = "adk" if "attributes" in field_names else "flat"
                    with self._schema_lock:
                        self._table_format_cache[table_ref] = detected
                    return detected
            except Exception:
                # Cache get_table().schema detection ONLY on success
                pass
        return "adk"

    def _should_flip_to_flat_on_error(
        self, client: Any, table_ref: str, err_text: str
    ) -> bool:
        err_lower = str(err_text or "").lower()
        if "no such field" not in err_lower:
            return False

        get_table_fn = getattr(client, "get_table", None)
        if callable(get_table_fn):
            try:
                tbl_obj = get_table_fn(table_ref)
                field_names = self._extract_schema_field_names(tbl_obj)
                if field_names:
                    if "attributes" not in field_names:
                        return True
                    with self._schema_lock:
                        self._table_format_cache[table_ref] = "adk"
                    return False
            except Exception:
                return any(col in err_lower for col in _ADK_ONLY_COLUMNS)
        return any(col in err_lower for col in _ADK_ONLY_COLUMNS)

    def _flush_loop(self):
        batch_limit = max(
            1, min(int(self.config.batch_size or 10), self._max_chunk_size)
        )
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=self._flush_interval_seconds)
            except queue.Empty:
                continue

            batch = [item]
            try:
                while len(batch) < batch_limit:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                self._insert_to_bq(batch)
            except Exception as exc:
                print(f"[ERROR] BigQuerySink worker error: {exc}", flush=True)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _format_rows(
        self, batch: list[dict[str, Any]], row_format: str = "adk"
    ) -> list[dict[str, Any]]:
        rows_to_insert: list[dict[str, Any]] = []
        if row_format != "flat":
            for r in batch:
                inp = _safe_nonneg_int(r.get("input_tokens"))
                cac = _safe_nonneg_int(r.get("cached_tokens"))
                out = _safe_nonneg_int(r.get("output_tokens"))
                thk = _safe_nonneg_int(r.get("thinking_tokens"))
                tot = inp + cac + out + thk
                model = str(r.get("model_name") or self.config.default_model)
                app = str(r.get("app_name") or "agent_app")
                raw_json_val = r.get("raw_json")
                raw_meta: dict[str, Any] = (
                    raw_json_val if isinstance(raw_json_val, dict) else {}
                )
                agent_col = str(
                    r.get("sub_agent") or raw_meta.get("sub_agent") or app
                )
                flow_col = str(
                    r.get("orchestration_flow")
                    or raw_meta.get("orchestration_flow")
                    or ""
                )
                cost_val = float(r.get("total_cost", 0.0) or 0.0)
                sess = str(r.get("session_id") or "default_session")
                inv = str(
                    r.get("invocation_id") or f"inv_{int(time.time() * 1000)}"
                )
                raw_lat = r.get("latency_ms")
                lat_ms = int(raw_lat) if raw_lat not in (None, 0) else None
                latency_payload = (
                    json.dumps({"total_ms": lat_ms, "ttft_ms": None})
                    if lat_ms is not None
                    else None
                )

                row = {
                    "timestamp": r.get(
                        "timestamp",
                        datetime.datetime.now(datetime.UTC).isoformat(),
                    ),
                    "event_type": "LLM_RESPONSE",
                    "agent": agent_col,
                    "session_id": sess,
                    "invocation_id": inv,
                    "content": json.dumps(
                        {
                            "model_version": model,
                            "user_query": str(r.get("user_query", ""))[:2000],
                            "response": str(r.get("agent_response", ""))[:3000],
                            "usage": {
                                "prompt": inp + cac,
                                "completion": out,
                                "total": tot,
                            },
                        }
                    ),
                    "attributes": json.dumps(
                        {
                            "model": model,
                            "model_version": model,
                            "sub_agent": agent_col,
                            "orchestration_flow": flow_col,
                            "total_cost": cost_val,
                            "usage_prompt_tokens": inp + cac,
                            "usage_cached_tokens": cac,
                            "usage_completion_tokens": out,
                            "usage_thinking_tokens": thk,
                            "usage_total_tokens": tot,
                            "context_cache_hit_rate": round(
                                cac / (inp + cac), 4
                            )
                            if (inp + cac) > 0
                            else 0.0,
                            "tools_called": [
                                str(t) for t in (r.get("tools_called") or [])
                            ],
                            "skills_active": [
                                str(s) for s in (r.get("skills_active") or [])
                            ],
                            "usage_metadata": {
                                "prompt_token_count": inp + cac,
                                "cached_content_token_count": cac,
                                "candidates_token_count": out,
                                "thoughts_token_count": thk,
                                "total_token_count": tot,
                            },
                            "session_metadata": {
                                "app_name": app,
                            },
                        }
                    ),
                    "latency_ms": latency_payload,
                }
                rows_to_insert.append(row)
        else:
            for r in batch:
                raw_j = r.get("raw_json", {})
                if isinstance(raw_j, dict):
                    try:
                        raw_j_str = json.dumps(raw_j)
                    except TypeError:
                        raw_j_str = str(raw_j)
                else:
                    raw_j_str = str(raw_j)
                row = {
                    "timestamp": r.get(
                        "timestamp",
                        datetime.datetime.now(datetime.UTC).isoformat(),
                    ),
                    "session_id": str(r.get("session_id") or "default_session"),
                    "app_name": str(r.get("app_name") or "agent_app"),
                    "model_name": str(
                        r.get("model_name") or self.config.default_model
                    ),
                    "user_query": str(r.get("user_query", ""))[:2000],
                    "agent_response": str(r.get("agent_response", ""))[:2000],
                    "input_tokens": _safe_nonneg_int(r.get("input_tokens")),
                    "cached_tokens": _safe_nonneg_int(r.get("cached_tokens")),
                    "output_tokens": _safe_nonneg_int(r.get("output_tokens")),
                    "thinking_tokens": _safe_nonneg_int(
                        r.get("thinking_tokens")
                    ),
                    "total_cost": float(r.get("total_cost", 0.0) or 0.0),
                    "tools_called": [
                        str(t) for t in (r.get("tools_called") or [])
                    ],
                    "skills_active": [
                        str(s) for s in (r.get("skills_active") or [])
                    ],
                    "raw_json": raw_j_str,
                }
                rows_to_insert.append(row)
        return rows_to_insert

    def _insert_to_bq(self, batch: list[dict[str, Any]]):
        if not batch:
            return

        chunk_size = max(1, min(int(self._max_chunk_size), _MAX_BQ_CHUNK_SIZE))
        for offset in range(0, len(batch), chunk_size):
            pending_records = list(batch[offset : offset + chunk_size])
            pending: list[dict[str, Any]] | None = None
            active_fmt: str | None = None
            flat_retried = False
            last_error: Any = None
            table_ref = f"{self.project_id or '<unresolved>'}.{self.dataset_id}.{self.table_id}"
            attempt = 0

            while True:
                client: Any = None
                try:
                    client = self._get_client()
                    resolved_proj = self._resolve_project_id(client)
                    table_ref = (
                        f"{resolved_proj}.{self.dataset_id}.{self.table_id}"
                    )
                    if active_fmt is None:
                        active_fmt = self._detect_row_format(client, table_ref)
                    if pending is None:
                        try:
                            pending = self._format_rows(
                                pending_records, row_format=active_fmt
                            )
                        except Exception as fmt_exc:
                            last_error = fmt_exc
                            break

                    errors = client.insert_rows_json(table_ref, pending)
                    if not errors:
                        print(
                            f"[OK] Streamed {len(pending)} turn events to BigQuery table {table_ref}.",
                            flush=True,
                        )
                        pending_records = []
                        pending = []
                        break

                    last_error = errors
                    failed_indices = [
                        err["index"]
                        for err in errors
                        if isinstance(err, dict)
                        and isinstance(err.get("index"), int)
                        and 0 <= err["index"] < len(pending)
                    ]
                    if failed_indices:
                        dedup_indices = sorted(set(failed_indices))
                        pending_records = [
                            pending_records[idx] for idx in dedup_indices
                        ]
                        pending = [pending[idx] for idx in dedup_indices]

                    if (
                        active_fmt == "adk"
                        and not flat_retried
                        and self._should_flip_to_flat_on_error(
                            client, table_ref, json.dumps(errors, default=str)
                        )
                    ):
                        with self._schema_lock:
                            self._table_format_cache[table_ref] = "flat"
                        active_fmt = "flat"
                        flat_retried = True
                        pending = self._format_rows(
                            pending_records, row_format="flat"
                        )
                        continue
                except Exception as exc:
                    last_error = exc
                    if (
                        active_fmt == "adk"
                        and not flat_retried
                        and client is not None  # type: ignore[possibly-undefined]
                        and self._should_flip_to_flat_on_error(
                            client, table_ref, str(exc)
                        )
                    ):
                        with self._schema_lock:
                            self._table_format_cache[table_ref] = "flat"
                        active_fmt = "flat"
                        flat_retried = True
                        pending = self._format_rows(
                            pending_records, row_format="flat"
                        )
                        continue

                if pending_records and attempt < self._max_retries:
                    delay = self._retry_base_delay * (2**attempt)
                    if delay > 0:
                        time.sleep(delay)
                    attempt += 1
                    continue
                break

            if pending_records:
                with self._stats_lock:
                    self.dropped_rows_count += len(pending_records)
                print(
                    f"[ERROR] BigQuerySink dropped {len(pending_records)} rows after {attempt + 1} attempts "
                    f"to {table_ref}: {last_error}",
                    flush=True,
                )

    def fetch_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            client = self._get_client()
            resolved_proj = self._resolve_project_id(client)
            query = f"""
                SELECT * FROM `{resolved_proj}.{self.dataset_id}.{self.table_id}`
                ORDER BY timestamp DESC
                LIMIT @limit
            """
            from google.cloud import bigquery

            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("limit", "INT64", int(limit))
                ]
            )
            rows = client.query(query, job_config=job_config).result()
            pricing_engine = PricingEngine(self.config)
            default_model = str(
                self.config.default_model
                or "publishers/google/models/gemini-3.8-flash"
            )
            normalized: list[dict[str, Any]] = []
            for row in rows:
                raw_dict = (
                    dict(row.items()) if hasattr(row, "items") else dict(row)
                )
                normalized.append(
                    parse_bq_turn_record(
                        raw_dict,
                        default_model=default_model,
                        pricing_engine=pricing_engine,
                    )
                )
            return normalized
        except Exception as e:
            print(f"[ERROR] BigQuerySink fetch_recent error: {e}", flush=True)
            return []

    def _drain_remaining_queue(self) -> None:
        remaining: list[dict[str, Any]] = []
        while True:
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            try:
                self._insert_to_bq(remaining)
            finally:
                for _ in remaining:
                    self._queue.task_done()

    def flush(self) -> None:
        """Synchronously drains queued batches and waits for any dequeued/in-flight worker batch without closing the sink."""
        self._drain_remaining_queue()
        if self._worker.is_alive():
            self._queue.join()
        self._drain_remaining_queue()

    def close(self):
        with self._stats_lock:
            if self._closed:
                return
            self._closed = True
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=5.0)
        # Drain any remaining queued turns on shutdown in <= 500-row chunks
        self._drain_remaining_queue()


def create_sink(config: TokenomicsConfig) -> BaseSink:
    """Factory function to instantiate appropriate sink. Explicit config.sink always wins over config.mode."""
    effective_sink = (getattr(config, "sink", None) or "").strip().lower()
    if not effective_sink:
        effective_sink = (
            "bigquery"
            if str(getattr(config, "mode", "")).strip().lower() == "prod"
            else "memory"
        )
    if effective_sink == "bigquery":
        return BigQuerySink(config)
    return InMemorySink()
