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
Centralized Optimization Recommendation Templates & FinOps Advisor Prompts
for the ADK Tokenomics Plugin (`adk_tokenomics_plugin`).

Designed to work with any Google ADK agent architecture (`LlmAgent`, `SequentialAgent`,
`LoopAgent`, `ParallelAgent`, or custom `BaseAgent`). All pricing rates, discount percentages,
and model catalogs are dynamically injected from `PricingEngine` / `TokenomicsConfig` at runtime.

Users can customize these templates and prompts by:
1. Passing `optimization_prompts={...}` to `TokenControlTower(...)`, `TokenomicsConfig(...)`, or `TokenProfiler(...)`.
2. Calling `tower.set_optimization_prompt(rule_key, template_str)`.
3. Supplying a JSON file via `--prompts-file` in the CLI (`adk-tokenomics profile --prompts-file custom_prompts.json`).
"""

from typing import Any

DEFAULT_OPTIMIZATION_PROMPTS: dict[str, str] = {
    "context_caching": (
        "⚡ [Context Caching]: Large uncached prompt detected in {turns_str}.\n"
        "   -> Add `context_cache_config=ContextCacheConfig(min_tokens={min_cache_tokens}, ttl_seconds={cache_ttl_seconds})`\n"
        "   -> Active model rate card provides a {cache_discount_pct:.0f}% discount on cached prefix tokens (est. savings: ${estimated_cache_savings:.5f})."
    ),
    "history_compaction": (
        "📦 [History Compaction]: Multi-turn context is accumulating across {total_turns} turns.\n"
        "   -> Add `events_compaction_config=EventsCompactionConfig(compaction_interval={compaction_interval}, overlap_size={compaction_overlap})`\n"
        "   -> Compacts older conversation events to prevent unbounded prompt growth across multi-turn sessions."
    ),
    "modular_skills": (
        "🧩 [Modular Skills]: High uncached prompt volume detected ({total_fresh:,} fresh input tokens).\n"
        "   -> Move large static instructions or reference catalogs into on-demand ADK `SkillToolset` (`SKILL.md`) modules loaded via `load_skill` (est. savings: ${estimated_skills_savings:.5f}/session)."
    ),
    "thinking_budget": (
        "🧠 [Thinking Budget Tuning]: High reasoning token ratio detected ({total_thinking:,} thinking vs. {total_output:,} output tokens; {thinking_ratio_str}{heavy_agents_suffix}).\n"
        "   -> Configure `BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget={recommended_thinking_budget}))` or set `thinking_budget=0` on deterministic ADK agents."
    ),
    "model_tiering": (
        "🎯 [Sub-Agent Model Tiering]: Low-complexity turns detected ({lightweight_turns_str}).\n"
        "   -> Route lightweight ADK agents/turns to `{recommended_lite_model}` (${lite_input_rate:.4g}/1M input, ${lite_output_rate:.4g}/1M output) for an estimated {tiering_savings_pct:.0f}% (${tiering_savings_dollars:.5f}) cost reduction on those turns."
    ),
    "well_optimized": (
        "✨ Excellent! Session is well-optimized with active context management."
    ),
}

FINOPS_ADVISOR_SYSTEM_PROMPT: str = """You are the Principal ADK Tokenomics & FinOps Architect.
Analyze the provided Google Agent Development Kit (ADK) session telemetry and turn-by-turn token waterfall for any ADK agent workflow.
Provide concrete, actionable optimization recommendations across five pillars:
1. Prefix Context Caching (`ContextCacheConfig(min_tokens=..., ttl_seconds=...)`)
2. Multi-Turn Event Compaction (`EventsCompactionConfig(compaction_interval=..., overlap_size=...)`)
3. Planner & Thinking Budget Right-Sizing (`BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget=...))`)
4. Dynamic Model Tiering across the configured model catalog:
{available_models_catalog}
5. On-Demand Modular Skills (`SKILL.md` progressive disclosure)

Quantify the projected percentage and dollar savings per 1,000 sessions using the active model rate card above.
"""

FINOPS_ADVISOR_USER_TEMPLATE: str = """Analyze the following ADK Agent Session Telemetry:
- Session Title: {session_title}
- Total Turns: {total_turns}
- Fresh Input Tokens: {total_fresh:,}
- Cached Input Tokens: {total_cached:,} ({cache_hit_pct:.1f}% cache hit rate)
- Output Tokens: {total_output:,}
- Thinking Tokens: {total_thinking:,}
- Total Session Cost: ${total_cost:.5f}

Turn Breakdown:
{turns_breakdown}

Provide prioritized ADK optimization recommendations and estimated cost reduction.
"""


def get_optimization_prompts(
    custom_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Returns a merged dictionary of default and user-customized optimization prompt templates."""
    prompts = dict(DEFAULT_OPTIMIZATION_PROMPTS)
    if custom_overrides:
        for k, v in custom_overrides.items():
            if v is not None:
                prompts[str(k)] = str(v)
    return prompts


def build_finops_advisor_prompt(
    turns: list[dict[str, Any]],
    session_title: str = "Active Session",
    custom_system_prompt: str | None = None,
    custom_user_template: str | None = None,
    pricing_engine: Any | None = None,
) -> dict[str, str]:
    """
    Formats session turn telemetry into a structured `{system_instruction, user_prompt}` payload
    that can be passed to any ADK `LlmAgent` or LLM client for AI-assisted FinOps diagnosis.
    """
    from adk_tokenomics_plugin.core.pricing import (
        PricingEngine,
        normalize_turn_tokens,
    )

    if pricing_engine is None:
        pricing_engine = PricingEngine()

    total_fresh = 0
    total_cached = 0
    total_output = 0
    total_thinking = 0
    total_cost = 0.0
    lines = []

    for idx, t in enumerate(turns, 1):
        agent_label = t.get("agent") or t.get("app_name") or ""
        query = t.get("user_query") or t.get("query") or f"Turn {idx}"
        norm = normalize_turn_tokens(t)
        fresh_tok = norm["fresh_tokens"]
        cached_tok = norm["cached_tokens"]
        output_tok = norm["output_tokens"]
        think_tok = norm["thinking_tokens"]
        model_name = str(
            t.get("model_name")
            or t.get("model")
            or pricing_engine.config.default_model
            or "publishers/google/models/gemini-3.8-flash"
        )
        if (fresh_tok + cached_tok + output_tok + think_tok) > 0:
            cost = float(
                pricing_engine.calculate_cost(
                    model_name, fresh_tok, cached_tok, output_tok, think_tok
                )
            )
        else:
            cost_val = (
                t.get("total_cost")
                or t.get("estimated_cost")
                or t.get("cost")
                or 0.0
            )
            cost = float(cost_val)

        total_fresh += fresh_tok
        total_cached += cached_tok
        total_output += output_tok
        total_thinking += think_tok
        total_cost += cost
        prefix = (
            f"[{agent_label}] "
            if agent_label and not str(query).startswith("[")
            else ""
        )
        lines.append(
            f"  Turn {idx} ({prefix}{query}): model={model_name}, fresh_in={fresh_tok:,}, "
            f"cached_in={cached_tok:,}, out={output_tok:,}, think={think_tok:,}, cost=${cost:.5f}"
        )

    overall_prompt = total_fresh + total_cached
    cache_hit_pct = (
        (total_cached / overall_prompt * 100.0) if overall_prompt > 0 else 0.0
    )
    user_tmpl = custom_user_template or FINOPS_ADVISOR_USER_TEMPLATE
    user_prompt = user_tmpl.format(
        session_title=session_title,
        total_turns=len(turns),
        total_fresh=total_fresh,
        total_cached=total_cached,
        cache_hit_pct=cache_hit_pct,
        total_output=total_output,
        total_thinking=total_thinking,
        total_cost=total_cost,
        turns_breakdown="\n".join(lines) if lines else "  (no turns recorded)",
    )
    catalog_str = (
        pricing_engine.format_models_catalog()
        if hasattr(pricing_engine, "format_models_catalog")
        else ""
    )
    sys_tmpl = custom_system_prompt or FINOPS_ADVISOR_SYSTEM_PROMPT
    if "{available_models_catalog}" in sys_tmpl:
        sys_instruction = sys_tmpl.format(available_models_catalog=catalog_str)
    else:
        sys_instruction = sys_tmpl

    return {
        "system_instruction": sys_instruction,
        "user_prompt": user_prompt,
    }
