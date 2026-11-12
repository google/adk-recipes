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
Tokenomics SDK - FinOps Matrix Aggregator & Reporting Engine
"""

import math
import re
from collections.abc import Mapping
from typing import Any

from adk_tokenomics_plugin.core.sinks import parse_bq_turn_record

_TOKEN_SPLIT_RE = re.compile(r"[/:\-@_.\s]+")
_OPENAI_REASONING_BASENAME_RE = re.compile(r"^o(?:1|3|4)(?:$|[-_:@])")
_OTHER_VENDOR_PREFIXES = (
    "cohere/",
    "publishers/cohere/",
    "ai21/",
    "publishers/ai21/",
    "amazon/",
    "bedrock/amazon.",
)


def _safe_int(val: Any) -> int:
    """Safely converts nullable numeric fields to non-negative integers."""
    if val is None or isinstance(val, bool):
        return 0
    try:
        f = float(val)
        if not math.isfinite(f):
            return 0
        return max(0, int(f))
    except (ValueError, TypeError):
        return 0


def _safe_float(val: Any) -> float:
    """Safely converts nullable numeric fields to non-negative finite floats."""
    if val is None or isinstance(val, bool):
        return 0.0
    try:
        f = float(val)
        if not math.isfinite(f):
            return 0.0
        return max(0.0, f)
    except (ValueError, TypeError):
        return 0.0


class FinOpsAggregator:
    """Aggregates raw turn records into multi-dimensional executive FinOps summaries."""

    @staticmethod
    def classify_provider(model_name: Any) -> str:
        if model_name is None:
            return "Other"
        m = str(model_name).strip().lower()
        if not m:
            return "Other"

        if m.startswith(_OTHER_VENDOR_PREFIXES) or "command-r" in m:
            return "Other"

        tokens = {tok for tok in _TOKEN_SPLIT_RE.split(m) if tok}
        basename = m.rsplit("/", 1)[-1]

        # 1. Explicit Open-Weight / MaaS families & hosting prefixes (checked before OpenAI/Google
        #    so names like 'phi-4-o1-distill', 'google/gemma-3-27b-it', or 'qwen-pro' never collide)
        if (
            any(
                p in m
                for p in (
                    "llama",
                    "meta/",
                    "meta-llama/",
                    "deepseek",
                    "mistral",
                    "mixtral",
                    "qwen",
                    "gemma",
                    "ollama",
                    "vllm",
                    "open_weight",
                    "open-weight",
                    "together_ai",
                    "fireworks_ai",
                    "groq/",
                )
            )
            or "phi" in tokens
        ):
            return "Open-Weight / MaaS"

        # 2. Anthropic Claude
        if (
            "claude" in m
            or "anthropic" in m
            or bool(tokens & {"sonnet", "haiku", "opus"})
        ):
            return "Anthropic Claude"

        # 3. OpenAI (require explicit gpt/openai/chatgpt token or leading o1/o3/o4 basename)
        if (
            m.startswith(("openai/", "gpt-", "gpt4", "chatgpt-"))
            or bool(tokens & {"gpt", "openai", "chatgpt"})
            or bool(_OPENAI_REASONING_BASENAME_RE.match(basename))
        ):
            return "OpenAI"

        # 4. Google Gemini (avoid bare 'pro' substring collisions with non-Google models)
        if (
            m.startswith(("publishers/google/", "vertex_ai/gemini"))
            or "gemini" in m
            or bool(tokens & {"google", "flash", "argon", "palm"})
        ):
            return "Google Gemini"

        return "Other"

    @classmethod
    def aggregate_records(
        cls,
        records: list[dict[str, Any]],
        pricing_engine: Any | None = None,
    ) -> dict[str, Any]:
        total_spend = 0.0
        total_tokens = 0
        total_input = 0
        total_cached = 0
        total_output = 0
        total_thinking = 0
        valid_records = [r for r in (records or []) if isinstance(r, Mapping)]
        total_turns = len(valid_records)

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

        matrix_groups: dict[tuple, dict[str, Any]] = {}

        from adk_tokenomics_plugin.core.pricing import PricingEngine

        pricing = (
            pricing_engine if pricing_engine is not None else PricingEngine()
        )
        default_model = str(
            getattr(getattr(pricing, "config", None), "default_model", None)
            or "gemini-3.8-flash"
        )
        savings_dollars = 0.0

        for raw_r in valid_records:
            is_bq_shaped = (
                "attributes" in raw_r
                or "content" in raw_r
                or "usage_prompt_tokens" in raw_r
                or "usage_completion_tokens" in raw_r
                or ("input_tokens" not in raw_r and "prompt_tokens" in raw_r)
            )
            r = (
                parse_bq_turn_record(
                    raw_r, default_model=default_model, pricing_engine=pricing
                )
                if is_bq_shaped
                else raw_r
            )

            in_tok = _safe_int(r.get("input_tokens"))
            cache_tok = _safe_int(r.get("cached_tokens"))
            out_tok = _safe_int(r.get("output_tokens"))
            think_tok = _safe_int(r.get("thinking_tokens"))
            tok_sum = in_tok + cache_tok + out_tok + think_tok

            raw_model = (
                r.get("model_name")
                if r.get("model_name") is not None
                else r.get("model_version")
            )
            model_id = (
                str(raw_model).strip()
                if raw_model is not None and str(raw_model).strip()
                else default_model
            )
            matrix_model_label = (
                str(raw_model).strip()
                if raw_model is not None and str(raw_model).strip()
                else "unknown"
            )
            raw_app = (
                r.get("app_name")
                if r.get("app_name") is not None
                else r.get("agent")
            )
            app_label = (
                str(raw_app).strip()
                if raw_app is not None and str(raw_app).strip()
                else "app"
            )

            if pricing_engine is not None and tok_sum > 0:
                cost = _safe_float(
                    pricing.calculate_cost(
                        model_id, in_tok, cache_tok, out_tok, think_tok
                    )
                )
            elif r.get("total_cost") is not None:
                cost = _safe_float(r.get("total_cost"))
            elif tok_sum > 0:
                cost = _safe_float(
                    pricing.calculate_cost(
                        model_id, in_tok, cache_tok, out_tok, think_tok
                    )
                )
            else:
                cost = 0.0

            total_spend += cost
            total_tokens += tok_sum
            total_input += in_tok
            total_cached += cache_tok
            total_output += out_tok
            total_thinking += think_tok

            rates = pricing.get_rate(model_id)
            in_rate = _safe_float(rates.get("input", 0.0))
            cache_rate = _safe_float(rates.get("cached", 0.0))
            savings_dollars += max(
                0.0, (cache_tok / 1_000_000.0) * (in_rate - cache_rate)
            )

            prov = cls.classify_provider(
                raw_model if raw_model is not None else model_id
            )
            provider_map.setdefault(
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
            provider_map[prov]["spend"] += cost
            provider_map[prov]["turns"] += 1
            provider_map[prov]["input"] += in_tok
            provider_map[prov]["cached"] += cache_tok
            provider_map[prov]["output"] += out_tok
            provider_map[prov]["thinking"] += think_tok

            key = (prov, matrix_model_label, app_label)
            if key not in matrix_groups:
                matrix_groups[key] = {
                    "provider": prov,
                    "model_name": key[1],
                    "app_name": key[2],
                    "turns": 0,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "total_tokens": 0,
                    "total_cost": 0.0,
                }
            g = matrix_groups[key]
            g["turns"] += 1
            g["input_tokens"] += in_tok
            g["cached_tokens"] += cache_tok
            g["output_tokens"] += out_tok
            g["thinking_tokens"] += think_tok
            g["total_tokens"] += tok_sum
            g["total_cost"] += cost

        top_provider = "None"
        top_spend = 0.0
        for p, data in provider_map.items():
            if data["spend"] > top_spend:
                top_spend = data["spend"]
                top_provider = p

        top_share = (
            (top_spend / total_spend * 100.0) if total_spend > 0 else 0.0
        )
        blended_cost = (
            (total_spend / total_tokens * 1_000_000.0)
            if total_tokens > 0
            else 0.0
        )

        hypothetical_baseline = total_spend + savings_dollars
        savings_pct = (
            (savings_dollars / hypothetical_baseline * 100.0)
            if hypothetical_baseline > 0
            else 0.0
        )

        matrix_rows = []
        for g in sorted(
            matrix_groups.values(), key=lambda x: x["total_cost"], reverse=True
        ):
            share = (
                (g["total_cost"] / total_spend * 100.0)
                if total_spend > 0
                else 0.0
            )
            g["spend_share_pct"] = round(share, 1)
            g["total_cost"] = round(g["total_cost"], 5)
            matrix_rows.append(g)

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
        }
