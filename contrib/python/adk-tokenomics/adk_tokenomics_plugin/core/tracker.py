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

"""
Tokenomics SDK - Core TokenTracker Engine
Extracts token usage, computes costs, calculates cache ratios, and dispatches turns to sinks.
"""

import datetime
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from adk_tokenomics_plugin.core.config import TokenomicsConfig
from adk_tokenomics_plugin.core.pricing import PricingEngine, _safe_nonneg_int
from adk_tokenomics_plugin.core.sinks import BaseSink, create_sink

logger = logging.getLogger(__name__)


class TokenTracker:
    def __init__(
        self,
        config: TokenomicsConfig | None = None,
        sink: BaseSink | None = None,
    ):
        self.config = config or TokenomicsConfig()
        self.pricing = PricingEngine(self.config)
        self.sink = sink or create_sink(self.config)
        self._lock = threading.RLock()
        self._otel_token_histogram = None
        self._otel_cost_histogram = None
        self._init_otel_instruments()

        # Session metrics (live in-process turns only)
        self.total_input_tokens = 0
        self.total_cached_tokens = 0
        self.total_output_tokens = 0
        self.total_thinking_tokens = 0
        self.total_cost = 0.0
        self.total_turns = 0
        self.total_latency_ms = 0
        self.turns_history: list[dict[str, Any]] = []
        self._hydrated_turns_by_session: dict[str, list[dict[str, Any]]] = {}
        self._archived_turns: list[dict[str, Any]] = []

    def _init_otel_instruments(self) -> None:
        """Initializes OpenTelemetry GenAI FinOps metric instruments to complement ADK's built-in MeterProvider."""
        try:
            from opentelemetry import metrics as otel_metrics  # type: ignore

            meter = otel_metrics.get_meter("google.adk.tokenomics")
            self._otel_cost_histogram = meter.create_histogram(
                name="gen_ai.client.cost.usd",
                unit="USD",
                description="Estimated USD cost and context cache savings per model generate_content operation.",
            )
        except Exception:
            logger.debug(
                "Failed to initialize OpenTelemetry cost histogram",
                exc_info=True,
            )
            self._otel_cost_histogram = None

    def _emit_otel_metrics(
        self,
        agent_name: str,
        model_name: str,
        total_cost: float,
        cache_savings: float = 0.0,
    ) -> None:
        """Emits complementary FinOps USD cost and cache savings metrics to the active OpenTelemetry MeterProvider."""
        if self._otel_cost_histogram is None:
            return
        try:
            base_attrs = {
                "gen_ai.operation.name": "generate_content",
                "gen_ai.agent.name": str(agent_name or "agent"),
                "gen_ai.response.model": str(model_name or "unknown"),
            }
            if total_cost >= 0:
                self._otel_cost_histogram.record(
                    float(total_cost),
                    attributes={**base_attrs, "gen_ai.cost.type": "actual"},
                )
            if cache_savings > 0:
                self._otel_cost_histogram.record(
                    float(cache_savings),
                    attributes={
                        **base_attrs,
                        "gen_ai.cost.type": "cache_savings",
                    },
                )
        except Exception:
            logger.debug(
                "Failed to record OpenTelemetry cost metric",
                exc_info=True,
            )

    @staticmethod
    def _turn_dedup_key(t: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(t.get("session_id") or ""),
            str(t.get("invocation_id") or ""),
            str(t.get("timestamp") or ""),
            str(t.get("sub_agent") or ""),
            str(t.get("model_name") or ""),
            _safe_nonneg_int(t.get("input_tokens")),
            _safe_nonneg_int(t.get("cached_tokens")),
            _safe_nonneg_int(t.get("output_tokens")),
            _safe_nonneg_int(t.get("thinking_tokens")),
        )

    def _apply_history_cap_locked(self) -> None:
        max_turns = getattr(self.config, "max_history_turns", None)
        if max_turns is not None and int(max_turns) > 0:
            cap = int(max_turns)
            while len(self.turns_history) > cap:
                evicted = self.turns_history.pop(0)
                self.total_input_tokens = max(
                    0,
                    self.total_input_tokens
                    - _safe_nonneg_int(evicted.get("input_tokens")),
                )
                self.total_cached_tokens = max(
                    0,
                    self.total_cached_tokens
                    - _safe_nonneg_int(evicted.get("cached_tokens")),
                )
                self.total_output_tokens = max(
                    0,
                    self.total_output_tokens
                    - _safe_nonneg_int(evicted.get("output_tokens")),
                )
                self.total_thinking_tokens = max(
                    0,
                    self.total_thinking_tokens
                    - _safe_nonneg_int(evicted.get("thinking_tokens")),
                )
                self.total_cost = max(
                    0.0,
                    self.total_cost
                    - float(evicted.get("total_cost", 0.0) or 0.0),
                )
                self.total_turns = max(0, self.total_turns - 1)
                self.total_latency_ms = max(
                    0,
                    self.total_latency_ms
                    - _safe_nonneg_int(evicted.get("latency_ms")),
                )

    @staticmethod
    def _parse_iso_epoch(ts_val: Any) -> float | None:
        if not ts_val:
            return None
        try:
            raw = str(ts_val).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt.timestamp()
        except Exception:
            return None

    def merge_historical_turns(self, prior_turns: list[dict[str, Any]]) -> int:
        """Stores prior persisted disk turns in the hydrated archive buffer without mutating live in-process counters."""
        if not prior_turns:
            return 0
        with self._lock:
            existing_keys = {
                self._turn_dedup_key(t)
                for t in self._archived_turns
                if isinstance(t, dict)
            } | {
                self._turn_dedup_key(t)
                for t in self.turns_history
                if isinstance(t, dict)
            }
            added: list[dict[str, Any]] = []
            for pt in prior_turns:
                if not isinstance(pt, dict):
                    continue
                key = self._turn_dedup_key(pt)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                copied = dict(pt)
                added.append(copied)

            if not added:
                return 0

            self._archived_turns.extend(added)
            max_turns = getattr(self.config, "max_history_turns", None)
            if max_turns is not None and int(max_turns) > 0:
                cap = int(max_turns)
                if len(self._archived_turns) > cap:
                    self._archived_turns = self._archived_turns[-cap:]
                    rebuilt: dict[str, list[dict[str, Any]]] = {}
                    for ht in self._archived_turns:
                        sid = str(ht.get("session_id") or "")
                        rebuilt.setdefault(sid, []).append(ht)
                    self._hydrated_turns_by_session = rebuilt
                    return len(added)

            for ht in added:
                sid = str(ht.get("session_id") or "")
                self._hydrated_turns_by_session.setdefault(sid, []).append(ht)
            return len(added)

    def record_turn(
        self,
        session_id: str,
        app_name: str,
        model_name: str,
        user_query: str,
        agent_response: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        tools_called: list[str] | None = None,
        skills_active: list[str] | None = None,
        raw_json: Any | None = None,
        latency_ms: int | None = None,
        invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Records a single LLM turn and persists to configured telemetry sink."""
        in_tok = _safe_nonneg_int(input_tokens)
        cache_tok = _safe_nonneg_int(cached_tokens)
        out_tok = _safe_nonneg_int(output_tokens)
        think_tok = _safe_nonneg_int(thinking_tokens)

        cost_info = self.pricing.calculate_turn_cost(
            model_id=model_name,
            input_tokens=in_tok,
            cached_tokens=cache_tok,
            output_tokens=out_tok,
            thinking_tokens=think_tok,
        )

        resolved_latency = _safe_nonneg_int(latency_ms)
        raw_meta = raw_json if isinstance(raw_json, dict) else {}
        resolved_sub_agent = str(raw_meta.get("sub_agent") or app_name)
        resolved_flow = str(raw_meta.get("orchestration_flow") or "")

        turn_record = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "session_id": session_id,
            "invocation_id": invocation_id or f"inv_{int(time.time() * 1000)}",
            "app_name": app_name,
            "sub_agent": resolved_sub_agent,
            "orchestration_flow": resolved_flow,
            "model_name": model_name,
            "user_query": user_query,
            "agent_response": agent_response,
            "input_tokens": in_tok,
            "cached_tokens": cache_tok,
            "output_tokens": out_tok,
            "thinking_tokens": think_tok,
            "total_tokens": in_tok + cache_tok + out_tok + think_tok,
            "total_cost": cost_info["total_cost"],
            "cost_breakdown": cost_info,
            "latency_ms": resolved_latency,
            "tools_called": list(tools_called) if tools_called else [],
            "skills_active": list(skills_active) if skills_active else [],
            "raw_json": raw_json if raw_json is not None else {},
        }

        # Update in-memory session totals (thread-safe for ParallelAgent)
        with self._lock:
            self.total_input_tokens += in_tok
            self.total_cached_tokens += cache_tok
            self.total_output_tokens += out_tok
            self.total_thinking_tokens += think_tok
            self.total_cost += cost_info["total_cost"]
            self.total_turns += 1
            self.total_latency_ms += resolved_latency
            self.turns_history.append(turn_record)
            self._apply_history_cap_locked()

        # Dispatch to sink and active OpenTelemetry MeterProvider
        self.sink.write_turn(turn_record)
        sub_agent_label = (
            raw_json.get("sub_agent")
            if isinstance(raw_json, dict) and raw_json.get("sub_agent")
            else app_name
        )
        self._emit_otel_metrics(
            agent_name=str(sub_agent_label),
            model_name=model_name,
            total_cost=cost_info["total_cost"],
            cache_savings=float(cost_info.get("savings_dollars", 0.0)),
        )
        return turn_record

    @staticmethod
    def _build_summary_dict(
        total_turns: int,
        in_toks: int,
        cached_toks: int,
        out_toks: int,
        think_toks: int,
        tot_cost: float,
        tot_lat: int,
    ) -> dict[str, Any]:
        total_in = in_toks + cached_toks
        cache_ratio = (cached_toks / total_in * 100.0) if total_in > 0 else 0.0
        total_toks = in_toks + cached_toks + out_toks + think_toks
        blended_cost = (
            (tot_cost / total_toks * 1_000_000.0) if total_toks > 0 else 0.0
        )
        avg_latency = (tot_lat / total_turns) if total_turns > 0 else 0.0
        return {
            "total_turns": total_turns,
            "total_cost": tot_cost,
            "total_tokens": total_toks,
            "input_tokens": in_toks,
            "cached_tokens": cached_toks,
            "output_tokens": out_toks,
            "thinking_tokens": think_toks,
            "cache_read_ratio_pct": cache_ratio,
            "blended_cost_per_1m": blended_cost,
            "total_latency_ms": tot_lat,
            "avg_latency_ms": avg_latency,
        }

    @classmethod
    def _summarize_turns(cls, turns: list[dict[str, Any]]) -> dict[str, Any]:
        total_turns = len(turns)
        in_toks = sum(_safe_nonneg_int(t.get("input_tokens")) for t in turns)
        cached_toks = sum(
            _safe_nonneg_int(t.get("cached_tokens")) for t in turns
        )
        out_toks = sum(_safe_nonneg_int(t.get("output_tokens")) for t in turns)
        think_toks = sum(
            _safe_nonneg_int(t.get("thinking_tokens")) for t in turns
        )
        tot_cost = sum(float(t.get("total_cost", 0.0) or 0.0) for t in turns)
        tot_lat = sum(_safe_nonneg_int(t.get("latency_ms")) for t in turns)
        return cls._build_summary_dict(
            total_turns=total_turns,
            in_toks=in_toks,
            cached_toks=cached_toks,
            out_toks=out_toks,
            think_toks=think_toks,
            tot_cost=tot_cost,
            tot_lat=tot_lat,
        )

    def _get_merged_turns_for_export_locked(
        self, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        if session_id:
            hydrated = list(self._hydrated_turns_by_session.get(session_id, []))
            live = [
                t
                for t in self.turns_history
                if t.get("session_id") == session_id
            ]
        else:
            hydrated = list(self._archived_turns)
            live = list(self.turns_history)

        indexed_unique: list[tuple[int, int, dict[str, Any]]] = []
        seen_keys = set()
        seq_idx = 0
        for bucket_rank, src_list in ((0, hydrated), (1, live)):
            for t in src_list:
                if not isinstance(t, dict):
                    continue
                k = self._turn_dedup_key(t)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                indexed_unique.append((bucket_rank, seq_idx, t))
                seq_idx += 1

        if len(indexed_unique) > 1 and all(
            self._parse_iso_epoch(item[2].get("timestamp")) is not None
            for item in indexed_unique
        ):
            indexed_unique.sort(
                key=lambda item: (
                    self._parse_iso_epoch(item[2].get("timestamp")) or 0.0,
                    item[1],
                )
            )
        else:
            indexed_unique.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in indexed_unique]

    def export_traces(
        self,
        file_path: str,
        session_id: str | None = None,
        compact: bool = True,
    ) -> str:
        """Atomically exports merged historical + live session turns to a JSON trace artifact file."""
        abs_path = os.path.abspath(file_path)
        target_dir = os.path.dirname(abs_path) or "."
        os.makedirs(target_dir, exist_ok=True)

        with self._lock:
            turns = self._get_merged_turns_for_export_locked(
                session_id=session_id
            )
            summary = self._summarize_turns(turns)

            payload: dict[str, Any] = {
                "summary": summary,
                "turns": turns,
            }
            if session_id:
                payload["session_id"] = session_id

            serialized = (
                json.dumps(payload, allow_nan=False, separators=(",", ":"))
                if compact
                else json.dumps(payload, indent=2, allow_nan=False)
            )

            tmp_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target_dir,
                    prefix=".tmp_trace_",
                    suffix=".json",
                    delete=False,
                ) as tf:
                    tmp_path = tf.name
                    tf.write(serialized)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_path, abs_path)
                tmp_path = None
            except Exception:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        return file_path

    def get_session_summary(
        self, session_id: str | None = None
    ) -> dict[str, Any]:
        """Alias for get_summary returning live in-process session metrics."""
        return self.get_summary(session_id=session_id)

    def get_summary(self, session_id: str | None = None) -> dict[str, Any]:
        """Returns session summary metrics (optionally scoped to a specific session_id)."""
        with self._lock:
            if session_id:
                turns = [
                    t
                    for t in self.turns_history
                    if t.get("session_id") == session_id
                ]
                total_turns = len(turns)
                in_toks = sum(
                    _safe_nonneg_int(t.get("input_tokens")) for t in turns
                )
                cached_toks = sum(
                    _safe_nonneg_int(t.get("cached_tokens")) for t in turns
                )
                out_toks = sum(
                    _safe_nonneg_int(t.get("output_tokens")) for t in turns
                )
                think_toks = sum(
                    _safe_nonneg_int(t.get("thinking_tokens")) for t in turns
                )
                tot_cost = sum(
                    float(t.get("total_cost", 0.0) or 0.0) for t in turns
                )
                tot_lat = sum(
                    _safe_nonneg_int(t.get("latency_ms")) for t in turns
                )
            else:
                total_turns = self.total_turns
                in_toks = self.total_input_tokens
                cached_toks = self.total_cached_tokens
                out_toks = self.total_output_tokens
                think_toks = self.total_thinking_tokens
                tot_cost = self.total_cost
                tot_lat = self.total_latency_ms

        return self._build_summary_dict(
            total_turns=total_turns,
            in_toks=in_toks,
            cached_toks=cached_toks,
            out_toks=out_toks,
            think_toks=think_toks,
            tot_cost=tot_cost,
            tot_lat=tot_lat,
        )
