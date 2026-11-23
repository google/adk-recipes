"""
Tokenomics SDK - Rate Card & Pricing Engine
Calculates granular turn costs and optimization ROI across all LLM vendors.
"""

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

from adk_tokenomics_plugin.core.config import (
    DEFAULT_MODELS_CONFIG,
    TokenomicsConfig,
    canonicalize_model_id,
)

logger = logging.getLogger(__name__)

_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:projects/[^/]+/locations/[^/]+/|publishers/[^/]+/models/|models/|vertex_ai/|anthropic/|openai/|together_ai/|fireworks_ai/|groq/|bedrock/|azure/|huggingface/)+"
)
_PRO_TOKEN_RE = re.compile(r"(?:^|[-_.])pro(?:$|[-_.])")
_OPENAI_REASONING_RE = re.compile(r"^(?:o1|o3|o4)(?:$|[-_:@])")
_PREFIX_BOUNDARY_CHARS = ("-", "@", ":", "_", ".")
_BOUNDARY_TOKEN_SPLIT_RE = re.compile(r"[-@:_.]")
_TIER_SUFFIX_TOKENS = {
    "mini",
    "nano",
    "lite",
    "small",
    "flash",
    "haiku",
    "micro",
    "pro",
    "opus",
    "sonnet",
    "hydrogen",
    "argon",
    "neon",
}


def _safe_nonneg_int(val: Any) -> int:
    """Safely converts token counts to non-negative integers, clamping None and negatives to 0."""
    if val is None or isinstance(val, bool):
        return 0
    try:
        return max(0, int(val))
    except (ValueError, TypeError, OverflowError):
        return 0


def normalize_turn_tokens(rt: Mapping[str, Any]) -> Dict[str, int]:
    """Normalizes heterogeneous turn token dicts into canonical fresh, cached, prompt, output, and thinking counts."""
    if not isinstance(rt, Mapping):
        return {
            "fresh_tokens": 0,
            "cached_tokens": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
        }
    cached_tok = _safe_nonneg_int(rt.get("cached_tokens"))
    if "input_tokens" in rt and ("total_tokens" in rt or "cost_breakdown" in rt):
        fresh_tok = _safe_nonneg_int(rt.get("input_tokens"))
    else:
        raw_prompt_val = rt.get("prompt_tokens") if rt.get("prompt_tokens") is not None else rt.get("input_tokens")
        raw_prompt = _safe_nonneg_int(raw_prompt_val)
        fresh_tok = max(0, raw_prompt - cached_tok) if raw_prompt >= cached_tok else raw_prompt

    output_val = rt.get("output_tokens") if rt.get("output_tokens") is not None else rt.get("completion_tokens")
    output_tok = _safe_nonneg_int(output_val)
    thinking_tok = _safe_nonneg_int(rt.get("thinking_tokens"))
    return {
        "fresh_tokens": fresh_tok,
        "cached_tokens": cached_tok,
        "prompt_tokens": fresh_tok + cached_tok,
        "output_tokens": output_tok,
        "thinking_tokens": thinking_tok,
    }


class PricingEngine:
    def __init__(self, config: Optional[TokenomicsConfig] = None):
        self.config = config or TokenomicsConfig()
        self._canonical_entries: List[Dict[str, Any]] = []
        self._prefix_keys: List[tuple[str, Dict[str, Any]]] = []
        self._warned_estimated_models: set[str] = set()
        self._load_rates()

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        """Strips LiteLLM and Vertex AI Model Garden routing prefixes from a model identifier."""
        cleaned = _PROVIDER_PREFIX_RE.sub("", str(model_id or "").strip())
        return cleaned

    def _load_rates(self):
        models_data = self.config.get_models_config()
        self.pricing_table: Dict[str, Dict[str, Any]] = {}
        canonical_by_id: Dict[str, Dict[str, Any]] = {}
        custom_ids = {canonicalize_model_id(str(k)) for k in (self.config.custom_pricing or {}).keys()}

        # 1. Load built-in / file-configured rate entries first so canonical short keys take priority over prefixed custom IDs
        for m in models_data.get("models", []):
            m_id = m.get("id")
            if not m_id or m_id in custom_ids:
                continue
            p = m.get("pricing", {})
            in_rate = float(p.get("input_per_million", p.get("input", 0.0)))
            cache_rate = float(p.get("cached_per_million", p.get("cached", 0.0)))
            out_rate = float(p.get("output_per_million", p.get("output", 0.0)))
            rate_entry = {
                "id": m_id,
                "name": m.get("name", m_id),
                "provider": m.get("provider", "google"),
                "input_per_million": in_rate,
                "cached_per_million": cache_rate,
                "output_per_million": out_rate,
                "input": in_rate,
                "cached": cache_rate,
                "output": out_rate,
            }
            canonical_by_id[m_id] = rate_entry
            self.pricing_table[m_id] = rate_entry
            norm_id = self._normalize_model_id(m_id)
            self.pricing_table[norm_id] = rate_entry
            if "@" in norm_id:
                self.pricing_table.setdefault(norm_id.split("@")[0], rate_entry)
            if "/" in norm_id:
                self.pricing_table.setdefault(norm_id.split("/")[-1], rate_entry)
            if m.get("name"):
                self.pricing_table[m["name"]] = rate_entry

        # 2. Apply custom_pricing overrides without clobbering existing canonical short aliases when registering a prefixed ID
        builtin_names = {
            m.get("id"): m.get("name")
            for m in DEFAULT_MODELS_CONFIG.get("models", [])
            if m.get("id") and m.get("name")
        }
        for raw_custom_id, custom_p in (self.config.custom_pricing or {}).items():
            custom_id = canonicalize_model_id(str(raw_custom_id))
            in_rate = float(custom_p.get("input_per_million", custom_p.get("input", 0.0)))
            cache_rate = float(custom_p.get("cached_per_million", custom_p.get("cached", in_rate * 0.25)))
            out_rate = float(custom_p.get("output_per_million", custom_p.get("output", 0.0)))
            raw_custom_name = custom_p.get("name")
            resolved_name = (
                raw_custom_name
                if (raw_custom_name and raw_custom_name != custom_id and raw_custom_name != raw_custom_id)
                else (builtin_names.get(custom_id) or raw_custom_name or custom_id)
            )
            rate_entry = {
                "id": custom_id,
                "name": resolved_name,
                "provider": custom_p.get("provider", "custom"),
                "input_per_million": in_rate,
                "cached_per_million": cache_rate,
                "output_per_million": out_rate,
                "input": in_rate,
                "cached": cache_rate,
                "output": out_rate,
            }
            canonical_by_id[custom_id] = rate_entry
            self.pricing_table[custom_id] = rate_entry
            norm_custom = self._normalize_model_id(custom_id)
            self.pricing_table[norm_custom] = rate_entry
            if "@" in norm_custom:
                self.pricing_table.setdefault(norm_custom.split("@")[0], rate_entry)
            if "/" in norm_custom:
                short_tail = norm_custom.split("/")[-1]
                self.pricing_table.setdefault(short_tail, rate_entry)
            if resolved_name:
                self.pricing_table.setdefault(str(resolved_name), rate_entry)

        self._canonical_entries = list(canonical_by_id.values())
        self._warned_estimated_models.clear()
        # Pre-sort normalized model-id keys longest-first for deterministic boundary-prefix matching
        prefix_candidates: Dict[str, Dict[str, Any]] = {}
        for k, v in self.pricing_table.items():
            if " " in k:
                continue
            prefix_candidates[k.lower()] = v
        self._prefix_keys = sorted(prefix_candidates.items(), key=lambda kv: len(kv[0]), reverse=True)

    def register_model(
        self,
        model_id: str,
        input_per_million: float,
        output_per_million: float,
        cached_per_million: Optional[float] = None,
        provider: str = "custom",
        name: Optional[str] = None,
        display_name: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dynamically registers a 3P, Open-Weight, or custom model rate card."""
        entry = self.config.register_model(
            model_id=model_id,
            input_per_million=input_per_million,
            output_per_million=output_per_million,
            cached_per_million=cached_per_million,
            provider=provider,
            name=name,
            display_name=display_name,
            tier=tier,
        )
        self._load_rates()
        return entry

    def _resolve_estimated_fallback(
        self,
        raw_id: str,
        canonical_id: Optional[str],
        fallback_spec: Dict[str, Any],
        match_kind: str,
    ) -> Dict[str, Any]:
        """Resolves a family or default fallback through the active rate card first, stamping pricing_estimated=True."""
        base = self.pricing_table.get(canonical_id) if canonical_id else None
        if base is not None:
            in_r = float(base["input_per_million"])
            cac_r = float(base["cached_per_million"])
            out_r = float(base["output_per_million"])
            name = str(base.get("name", fallback_spec["name"]))
            provider = str(base.get("provider", fallback_spec["provider"]))
        else:
            in_r = float(fallback_spec["input_per_million"])
            cac_r = float(fallback_spec["cached_per_million"])
            out_r = float(fallback_spec["output_per_million"])
            name = str(fallback_spec["name"])
            provider = str(fallback_spec["provider"])

        result: Dict[str, Any] = {
            "id": None,
            "matched_family_id": canonical_id,
            "name": name,
            "provider": provider,
            "input_per_million": in_r,
            "cached_per_million": cac_r,
            "output_per_million": out_r,
            "input": in_r,
            "cached": cac_r,
            "output": out_r,
            "rate_card_match": match_kind,
            "pricing_estimated": True,
        }
        if raw_id and raw_id not in self._warned_estimated_models:
            self._warned_estimated_models.add(raw_id)
            logger.warning(
                "Model %r is not in the active rate card; estimating USD with %s rates "
                "($%.4g / $%.4g / $%.4g per 1M tokens, match=%s). Register exact pricing in "
                "models_config.json or via register_model() to silence this warning.",
                raw_id,
                name,
                in_r,
                cac_r,
                out_r,
                match_kind,
            )
        return result

    def get_rate(self, model_id: str) -> Dict[str, Any]:
        """Returns pricing rate card for a given 1P, 3P (Claude, OpenAI), or Open-Weight model ID."""
        raw_id = str(model_id or "").strip()
        if raw_id in self.pricing_table:
            return {**self.pricing_table[raw_id], "rate_card_match": "exact", "pricing_estimated": False}

        norm_id = self._normalize_model_id(raw_id)
        if norm_id in self.pricing_table:
            return {**self.pricing_table[norm_id], "rate_card_match": "exact", "pricing_estimated": False}
        last_seg = norm_id.split("/")[-1] if "/" in norm_id else norm_id
        if last_seg in self.pricing_table:
            return {**self.pricing_table[last_seg], "rate_card_match": "exact", "pricing_estimated": False}

        # Case-insensitive exact lookup across registered keys
        mid_lower = raw_id.lower()
        norm_lower = norm_id.lower()
        seg_lower = last_seg.lower()
        for k, v in self.pricing_table.items():
            k_low = k.lower()
            if k_low in (mid_lower, norm_lower, seg_lower):
                return {**v, "rate_card_match": "exact", "pricing_estimated": False}

        # Longest-prefix match against configured model keys at version/date boundary delimiters (-, @, :, _, .),
        # skipping parent matches when any suffix token denotes a distinct model tier (e.g. gpt-5-mini, gpt-5-pro).
        for k_low, v in self._prefix_keys:
            for target in (norm_lower, seg_lower):
                if (
                    len(target) > len(k_low)
                    and target.startswith(k_low)
                    and target[len(k_low)] in _PREFIX_BOUNDARY_CHARS
                ):
                    rem = target[len(k_low) :].lstrip("-@:_.")
                    rem_tokens = [tok for tok in _BOUNDARY_TOKEN_SPLIT_RE.split(rem) if tok]
                    if any(tok in _TIER_SUFFIX_TOKENS for tok in rem_tokens):
                        continue
                    return {**v, "rate_card_match": "prefix", "pricing_estimated": False}

        # Local self-hosted runtime prefix check
        if mid_lower.startswith(("ollama/", "vllm/", "hosted_vllm/", "local/")):
            local_base = self.pricing_table.get("ollama/local-open-weight")
            if local_base is not None:
                return {**local_base, "rate_card_match": "local_open_weight", "pricing_estimated": False}
            return {
                "id": None,
                "name": "Local Open-Weight (Ollama/vLLM)",
                "provider": "open_weight",
                "input_per_million": 0.0,
                "cached_per_million": 0.0,
                "output_per_million": 0.0,
                "input": 0.0,
                "cached": 0.0,
                "output": 0.0,
                "rate_card_match": "local_open_weight",
                "pricing_estimated": False,
            }

        # Family fallback heuristics run strictly on the final '/' path segment (seg_lower),
        # delegating to active pricing_table canonical entries first so Rate Card edits propagate.
        if "haiku" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "claude-3-5-haiku",
                {"name": "Claude Haiku", "provider": "anthropic", "input_per_million": 0.80, "cached_per_million": 0.08, "output_per_million": 4.00},
                "family_heuristic",
            )
        if "opus" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "claude-opus-4",
                {"name": "Claude Opus", "provider": "anthropic", "input_per_million": 15.00, "cached_per_million": 1.50, "output_per_million": 75.00},
                "family_heuristic",
            )
        if "claude" in seg_lower or "sonnet" in seg_lower:
            sonnet_key = "claude-3-7-sonnet" if "claude-3-7-sonnet" in self.pricing_table else "claude-sonnet-5"
            return self._resolve_estimated_fallback(
                raw_id,
                sonnet_key,
                {"name": "Claude Sonnet", "provider": "anthropic", "input_per_million": 3.00, "cached_per_million": 0.30, "output_per_million": 15.00},
                "family_heuristic",
            )
        if "llama" in seg_lower:
            if "8b" in seg_lower:
                return self._resolve_estimated_fallback(
                    raw_id,
                    "meta/llama-3.1-8b-instruct",
                    {"name": "Llama 8B (Open Weight)", "provider": "open_weight", "input_per_million": 0.05, "cached_per_million": 0.01, "output_per_million": 0.08},
                    "family_heuristic",
                )
            if "405b" in seg_lower:
                return self._resolve_estimated_fallback(
                    raw_id,
                    "meta/llama-3.1-405b-instruct",
                    {"name": "Llama 405B (Open Weight)", "provider": "open_weight", "input_per_million": 1.80, "cached_per_million": 0.45, "output_per_million": 1.80},
                    "family_heuristic",
                )
            return self._resolve_estimated_fallback(
                raw_id,
                "meta/llama-3.3-70b-instruct",
                {"name": "Llama 70B (Open Weight)", "provider": "open_weight", "input_per_million": 0.60, "cached_per_million": 0.15, "output_per_million": 0.60},
                "family_heuristic",
            )
        if "deepseek" in seg_lower:
            if "r1" in seg_lower:
                return self._resolve_estimated_fallback(
                    raw_id,
                    "deepseek/deepseek-r1",
                    {"name": "DeepSeek R1 (Open Weight)", "provider": "open_weight", "input_per_million": 0.55, "cached_per_million": 0.14, "output_per_million": 2.19},
                    "family_heuristic",
                )
            return self._resolve_estimated_fallback(
                raw_id,
                "deepseek/deepseek-v3",
                {"name": "DeepSeek V3 (Open Weight)", "provider": "open_weight", "input_per_million": 0.27, "cached_per_million": 0.07, "output_per_million": 1.10},
                "family_heuristic",
            )
        if "mistral" in seg_lower or "mixtral" in seg_lower:
            if "small" in seg_lower:
                return self._resolve_estimated_fallback(
                    raw_id,
                    "mistralai/mistral-small",
                    {"name": "Mistral Small (Open Weight)", "provider": "open_weight", "input_per_million": 0.10, "cached_per_million": 0.025, "output_per_million": 0.30},
                    "family_heuristic",
                )
            return self._resolve_estimated_fallback(
                raw_id,
                "mistralai/mistral-large",
                {"name": "Mistral Large (Open Weight)", "provider": "open_weight", "input_per_million": 2.00, "cached_per_million": 0.50, "output_per_million": 6.00},
                "family_heuristic",
            )
        if "qwen" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "qwen/qwen-2.5-72b-instruct",
                {"name": "Qwen (Open Weight)", "provider": "open_weight", "input_per_million": 0.35, "cached_per_million": 0.09, "output_per_million": 0.40},
                "family_heuristic",
            )
        if "gemma" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "google/gemma-3-27b-it",
                {"name": "Gemma (Open Weight)", "provider": "open_weight", "input_per_million": 0.10, "cached_per_million": 0.025, "output_per_million": 0.20},
                "family_heuristic",
            )
        if "gpt" in seg_lower or _OPENAI_REASONING_RE.search(seg_lower):
            if "mini" in seg_lower or "nano" in seg_lower:
                return self._resolve_estimated_fallback(
                    raw_id,
                    "gpt-4o-mini",
                    {"name": "OpenAI Mini", "provider": "openai", "input_per_million": 0.15, "cached_per_million": 0.075, "output_per_million": 0.60},
                    "family_heuristic",
                )
            if _PRO_TOKEN_RE.search(seg_lower):
                return self._resolve_estimated_fallback(
                    raw_id,
                    None,
                    {"name": "OpenAI Pro", "provider": "openai", "input_per_million": 15.00, "cached_per_million": 3.75, "output_per_million": 120.00},
                    "family_heuristic",
                )
            if seg_lower.startswith("gpt-5"):
                return self._resolve_estimated_fallback(
                    raw_id,
                    "gpt-5",
                    {"name": "OpenAI GPT-5", "provider": "openai", "input_per_million": 1.25, "cached_per_million": 0.125, "output_per_million": 10.00},
                    "family_heuristic",
                )
            return self._resolve_estimated_fallback(
                raw_id,
                "gpt-4o",
                {"name": "GPT-4o", "provider": "openai", "input_per_million": 2.50, "cached_per_million": 1.25, "output_per_million": 10.00},
                "family_heuristic",
            )
        if "neon" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "publishers/google/models/gemini-4-neon",
                {"name": "Gemini 4 Neon", "provider": "google", "input_per_million": 4.00, "cached_per_million": 0.20, "output_per_million": 20.00},
                "family_heuristic",
            )
        if "argon" in seg_lower or _PRO_TOKEN_RE.search(seg_lower):
            return self._resolve_estimated_fallback(
                raw_id,
                "publishers/google/models/gemini-4-argon",
                {"name": "Gemini 4 Argon", "provider": "google", "input_per_million": 2.00, "cached_per_million": 0.10, "output_per_million": 10.00},
                "family_heuristic",
            )
        if "hydrogen" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "publishers/google/models/gemini-4-hydrogen",
                {"name": "Gemini 4 Hydrogen", "provider": "google", "input_per_million": 1.00, "cached_per_million": 0.10, "output_per_million": 4.00},
                "family_heuristic",
            )
        if "flash" in seg_lower or "gemini" in seg_lower:
            return self._resolve_estimated_fallback(
                raw_id,
                "publishers/google/models/gemini-3.8-flash",
                {"name": "Gemini 3.8 Flash", "provider": "google", "input_per_million": 0.75, "cached_per_million": 0.075, "output_per_million": 3.75},
                "family_heuristic",
            )

        # Default fallback rate for unrecognized model IDs
        return self._resolve_estimated_fallback(
            raw_id,
            "publishers/google/models/gemini-3.8-flash",
            {"name": "Gemini 3.8 Flash", "provider": "google", "input_per_million": 0.75, "cached_per_million": 0.075, "output_per_million": 3.75},
            "default_fallback",
        )

    def get_rates(self, model_id: str) -> Dict[str, Any]:
        """Alias for get_rate."""
        return self.get_rate(model_id)

    def calculate_turn_cost(
        self,
        model_id: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0
    ) -> Dict[str, float]:
        """Calculates precise dollar cost for a turn, clamping None and negative counts to 0."""
        rates = self.get_rate(model_id)
        in_tok = _safe_nonneg_int(input_tokens)
        cache_tok = _safe_nonneg_int(cached_tokens)
        out_tok = _safe_nonneg_int(output_tokens)
        think_tok = _safe_nonneg_int(thinking_tokens)

        input_cost = (in_tok / 1_000_000.0) * rates["input"]
        cached_cost = (cache_tok / 1_000_000.0) * rates["cached"]
        total_gen_tokens = out_tok + think_tok
        output_cost = (total_gen_tokens / 1_000_000.0) * rates["output"]

        total_cost = input_cost + cached_cost + output_cost
        naive_hypothetical = ((in_tok + cache_tok) / 1_000_000.0) * rates["input"] + output_cost
        savings_dollars = max(0.0, naive_hypothetical - total_cost)
        savings_pct = (savings_dollars / naive_hypothetical * 100.0) if naive_hypothetical > 0 else 0.0

        return {
            "input_cost": input_cost,
            "cached_cost": cached_cost,
            "output_cost": output_cost,
            "total_cost": total_cost,
            "hypothetical_uncached_cost": naive_hypothetical,
            "savings_dollars": savings_dollars,
            "savings_pct": savings_pct,
            "pricing_estimated": bool(rates.get("pricing_estimated", False)),
            "rate_card_match": str(rates.get("rate_card_match", "exact")),
            "rates_used": rates,
        }

    def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0
    ) -> float:
        """Returns the total scalar dollar cost for a turn."""
        return self.calculate_turn_cost(model_id, input_tokens, cached_tokens, output_tokens, thinking_tokens)["total_cost"]

    def get_cache_discount_pct(self, model_id: str) -> float:
        """Computes the exact context caching discount percentage for a given model based on its rate card."""
        rates = self.get_rate(model_id)
        in_rate = float(rates.get("input", 0.0))
        cache_rate = float(rates.get("cached", 0.0))
        if in_rate <= 0.0 or cache_rate >= in_rate:
            return 0.0
        return ((in_rate - cache_rate) / in_rate) * 100.0

    @staticmethod
    def _is_self_hosted_or_zero_cost(
        model_id: str,
        name: str,
        provider: str,
        in_rate: float,
        out_rate: float,
    ) -> bool:
        mid_low = model_id.lower()
        name_low = name.lower()
        prov_low = provider.lower()
        if mid_low.startswith(("ollama/", "vllm/", "hosted_vllm/", "local/")):
            return True
        if prov_low in ("self_hosted", "local", "ollama", "vllm"):
            return True
        if "self-hosted" in name_low or "self_hosted" in name_low:
            return True
        if in_rate <= 0.0 and out_rate <= 0.0:
            return True
        return False

    def get_cheapest_model(
        self,
        provider: Optional[str] = None,
        include_self_hosted: bool = False,
    ) -> Dict[str, Any]:
        """Returns the lowest-cost configured model (matching provider when given) from the active rate card."""
        candidates = []
        seen_ids = set()
        source_entries = self._canonical_entries or list(self.pricing_table.values())
        req_provider = str(provider).strip().lower() if (provider is not None and str(provider).strip()) else None

        for entry in source_entries:
            m_id = str(entry.get("id") or entry.get("name") or "")
            if not m_id or m_id in seen_ids:
                continue
            entry_provider = str(entry.get("provider", "")).lower()
            if req_provider is not None and entry_provider != req_provider:
                continue
            in_r = float(entry.get("input_per_million", 0.0))
            cac_r = float(entry.get("cached_per_million", 0.0))
            out_r = float(entry.get("output_per_million", 0.0))
            entry_name = str(entry.get("name", m_id))
            if not include_self_hosted and self._is_self_hosted_or_zero_cost(
                m_id, entry_name, entry_provider, in_r, out_r
            ):
                continue
            seen_ids.add(m_id)
            short_id = m_id.replace("publishers/google/models/", "").replace("models/", "")
            candidates.append({
                "id": m_id,
                "short_id": short_id,
                "name": entry_name,
                "provider": entry.get("provider", "google"),
                "input_per_million": in_r,
                "cached_per_million": cac_r,
                "output_per_million": out_r,
            })

        if not candidates:
            # When an explicit provider has no eligible cloud models, fall back to configured default_model
            # rather than returning a $0 local self-hosted ollama entry.
            default_id = str(self.config.default_model or "publishers/google/models/gemini-3.8-flash")
            fallback = self.get_rate(default_id)
            canonical_id = str(fallback.get("id") or default_id)
            fb_in = float(fallback["input_per_million"])
            fb_out = float(fallback["output_per_million"])
            if not include_self_hosted and self._is_self_hosted_or_zero_cost(
                canonical_id, str(fallback.get("name", "")), str(fallback.get("provider", "")), fb_in, fb_out
            ):
                canonical_id = "publishers/google/models/gemini-3.8-flash"
                fallback = self.get_rate(canonical_id)
            return {
                "id": canonical_id,
                "short_id": canonical_id.replace("publishers/google/models/", "").replace("models/", ""),
                "name": fallback["name"],
                "provider": fallback["provider"],
                "input_per_million": float(fallback["input_per_million"]),
                "cached_per_million": float(fallback["cached_per_million"]),
                "output_per_million": float(fallback["output_per_million"]),
            }

        candidates.sort(key=lambda m: (m["input_per_million"], m["output_per_million"]))
        return candidates[0]

    def format_models_catalog(self) -> str:
        """Formats the active model rate card into a summary string for FinOps advisor prompts."""
        seen = set()
        lines = []
        source_entries = self._canonical_entries or list(self.pricing_table.values())
        for entry in source_entries:
            m_id = str(entry.get("id") or entry.get("name") or "")
            name = entry.get("name", m_id)
            if name in seen:
                continue
            seen.add(name)
            short_id = m_id.replace("publishers/google/models/", "").replace("models/", "")
            in_r = float(entry.get("input_per_million", 0.0))
            cac_r = float(entry.get("cached_per_million", 0.0))
            out_r = float(entry.get("output_per_million", 0.0))
            disc = ((in_r - cac_r) / in_r * 100.0) if in_r > 0 else 0.0
            lines.append(
                f"  - `{short_id}` ({name}): input=${in_r:.4g}/1M, cached=${cac_r:.4g}/1M ({disc:.0f}% cache discount), output=${out_r:.4g}/1M"
            )
        return "\n".join(lines)
