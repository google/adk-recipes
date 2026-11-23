"""
ADK Tokenomics - CLI Context & Token Profiler
Provides microscopic turn-by-turn context waterfall, token decomposition, and automated ADK optimization recommendations.
"""

import dataclasses
import glob
import io
import json
import os
import sys
from contextlib import redirect_stdout
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from adk_tokenomics_plugin.core.config import (
    OptimizationRulesConfig,
    TokenomicsConfig,
    bigquery_install_hint,
    validate_bq_identifier,
    validate_bq_project_id,
)
from adk_tokenomics_plugin.core.pricing import PricingEngine, _safe_nonneg_int, normalize_turn_tokens
from adk_tokenomics_plugin.core.sinks import parse_bq_turn_record
from adk_tokenomics_plugin.prompts import get_optimization_prompts


class TokenProfiler:
    def __init__(
        self,
        pricing_engine: Optional[PricingEngine] = None,
        rules_config: Optional[OptimizationRulesConfig] = None,
        optimization_prompts: Optional[Dict[str, str]] = None,
        custom_callbacks: Optional[List[Callable[[Dict[str, Any]], Optional[str]]]] = None,
    ):
        self.pricing = pricing_engine or PricingEngine()
        self.rules = rules_config or OptimizationRulesConfig()
        self.prompts = get_optimization_prompts(optimization_prompts)
        self.custom_callbacks = list(custom_callbacks or [])

    @staticmethod
    def _extract_sub_agent_name(turn: Dict[str, Any], raw_query: str) -> str:
        """Extracts the sub-agent name from turn metadata or [agent] query prefix."""
        raw_meta = turn.get("raw_json") if isinstance(turn.get("raw_json"), dict) else {}
        explicit = turn.get("sub_agent") or turn.get("agent") or raw_meta.get("sub_agent")
        if explicit and str(explicit) not in ("local_agent", "customer_agent", "agent", "adk_agent"):
            return str(explicit)
        if raw_query.startswith("[") and "]" in raw_query:
            return raw_query.split("]")[0].lstrip("[")
        app_name = turn.get("app_name")
        if app_name and str(app_name) not in ("local_agent", "customer_agent", "agent", "adk_agent"):
            return str(app_name)
        return str(explicit or app_name or "root_agent")

    @staticmethod
    def _extract_turn_label(idx: int, turn: Dict[str, Any], raw_query: str) -> str:
        """Extracts a human-readable agent/turn label for any ADK agent trace."""
        raw_meta = turn.get("raw_json") if isinstance(turn.get("raw_json"), dict) else {}
        explicit_agent = turn.get("agent") or turn.get("sub_agent") or raw_meta.get("sub_agent")
        if explicit_agent and str(explicit_agent) not in ("local_agent", "customer_agent", "agent", "adk_agent"):
            return f"Turn #{idx} [{explicit_agent}]"
        if raw_query.startswith("[") and "]" in raw_query:
            return f"Turn #{idx} {raw_query.split(']')[0]}]"
        app_name = turn.get("app_name")
        if app_name and app_name not in ("local_agent", "customer_agent", "agent", "adk_agent"):
            return f"Turn #{idx} [{app_name}]"
        return f"Turn #{idx}"

    def profile_session(self, turns: List[Dict[str, Any]], session_title: str = "Session Profile") -> List[str]:
        """Renders an ANSI-formatted turn-by-turn context waterfall and returns optimization recommendations."""
        self.last_unregistered_models: List[str] = []
        self.last_pricing_estimated: bool = False
        if not turns:
            self.last_agent_rollup = {}
            print(f"\n[WARN] No turn records found to profile for '{session_title}'.\n")
            return []

        print("\n" + "=" * 105)
        print(f" 🔬 ADK TOKENOMICS PROFILER | {session_title}")
        print("=" * 105)

        header = f"{'Turn':<6} | {'User Prompt':<24} | {'Fresh In':<9} | {'Cached In':<9} | {'Output':<8} | {'Think':<7} | {'Cost ($)':<10} | {'Status / Cache'}"
        print(header)
        print("-" * 105)

        total_fresh = 0
        total_cached = 0
        max_prompt_tokens = 0
        total_output = 0
        total_thinking = 0
        total_cost = 0.0

        uncached_large_prompts = []
        uncached_candidates_raw = []
        cached_seen_by_agent: set[Tuple[str, str]] = set()
        first_large_idx_by_agent: Dict[Tuple[str, str], int] = {}
        heavy_thinking_turns = []
        lightweight_turns = []
        agent_max_output: Dict[str, int] = {}
        rapid_history_growth = False
        history_grew = False
        first_input_by_agent: Dict[Tuple[str, str], int] = {}
        prev_input_by_agent: Dict[Tuple[str, str], int] = {}
        prev_inv_by_agent: Dict[Tuple[str, str], Optional[str]] = {}
        agent_rollup: Dict[str, Dict[str, Any]] = {}
        session_seen_invs: Dict[str, set[str]] = {}
        session_none_turns: Dict[str, int] = {}
        unregistered_models: List[str] = []

        for idx, t in enumerate(turns, 1):
            raw_query = str(t.get("user_query") or t.get("query") or f"Turn {idx}")
            query = raw_query[:21] + "…" if len(raw_query) > 22 else raw_query

            # Note: normalize_turn_tokens supports both Google GenAI (prompt_tokens includes cached_tokens)
            # and Anthropic/LiteLLM semantics (when cached_tokens > prompt_tokens, fresh_tokens = prompt_tokens).
            norm = normalize_turn_tokens(t)
            fresh_tok = norm["fresh_tokens"]
            cached_tok = norm["cached_tokens"]
            output_tok = norm["output_tokens"]
            think_tok = norm["thinking_tokens"]
            model_name = str(t.get("model_name") or t.get("model") or self.pricing.config.default_model)

            rate_meta = self.pricing.get_rate(model_name)
            turn_price_est = bool(rate_meta.get("pricing_estimated")) or bool(t.get("pricing_estimated"))
            if turn_price_est and model_name not in unregistered_models:
                unregistered_models.append(model_name)

            if (fresh_tok + cached_tok + output_tok + think_tok) > 0:
                cost = self.pricing.calculate_cost(model_name, fresh_tok, cached_tok, output_tok, think_tok)
            else:
                cost = t.get("total_cost") or t.get("estimated_cost") or t.get("cost") or 0.0
            cost = float(cost)

            total_fresh += fresh_tok
            total_cached += cached_tok
            total_output += output_tok
            total_thinking += think_tok
            total_cost += cost
            max_prompt_tokens = max(max_prompt_tokens, fresh_tok + cached_tok)

            turn_label = self._extract_turn_label(idx, t, raw_query)
            sub_agent = self._extract_sub_agent_name(t, raw_query)
            raw_meta = t.get("raw_json") if isinstance(t.get("raw_json"), dict) else {}
            flow_stage = str(t.get("orchestration_flow") or raw_meta.get("orchestration_flow") or "Root")

            sess_key = str(t.get("session_id") or "default_session")
            raw_inv = t.get("invocation_id")
            inv_id = str(raw_inv).strip() if (raw_inv is not None and str(raw_inv).strip()) else None
            session_seen_invs.setdefault(sess_key, set())
            session_none_turns.setdefault(sess_key, 0)
            if inv_id is not None:
                session_seen_invs[sess_key].add(inv_id)
            else:
                session_none_turns[sess_key] += 1

            ag_entry = agent_rollup.setdefault(sub_agent, {
                "stage": flow_stage,
                "turns": 0,
                "fresh": 0,
                "cached": 0,
                "output": 0,
                "thinking": 0,
                "cost": 0.0,
            })
            if ag_entry["stage"] in ("", "Root") and flow_stage not in ("", "Root"):
                ag_entry["stage"] = flow_stage
            ag_entry["turns"] += 1
            ag_entry["fresh"] += fresh_tok
            ag_entry["cached"] += cached_tok
            ag_entry["output"] += output_tok
            ag_entry["thinking"] += think_tok
            ag_entry["cost"] += cost

            agent_key = (sess_key, sub_agent)
            agent_max_output[sub_agent] = max(agent_max_output.get(sub_agent, 0), output_tok)

            if cached_tok > 0:
                hit_rate = (cached_tok / (fresh_tok + cached_tok)) * 100
                status = f"🟢 Cached ({hit_rate:.0f}%)"
                cached_seen_by_agent.add(agent_key)
            elif fresh_tok > self.rules.min_cache_tokens:
                status = f"⚠️  No-Cache (>{self.rules.min_cache_tokens})"
                first_large_idx_by_agent.setdefault(agent_key, idx)
                uncached_candidates_raw.append((idx, fresh_tok, model_name, agent_key))
            else:
                status = "⚪ Direct Text"

            if think_tok > self.rules.recommended_thinking_budget and (
                output_tok == 0
                or (think_tok / output_tok) > self.rules.max_thinking_to_output_ratio
            ):
                heavy_thinking_turns.append((idx, turn_label, think_tok, output_tok, model_name))

            if (
                self.rules.enable_model_tiering_hint
                and think_tok <= self.rules.lightweight_turn_max_thinking_tokens
                and output_tok <= self.rules.lightweight_turn_max_output_tokens
            ):
                lightweight_turns.append({
                    "idx": idx,
                    "label": turn_label,
                    "sub_agent": sub_agent,
                    "model_name": model_name,
                    "fresh_tok": fresh_tok,
                    "cached_tok": cached_tok,
                    "output_tok": output_tok,
                    "think_tok": think_tok,
                    "cost": cost,
                })

            has_seen_agent = agent_key in prev_inv_by_agent
            prev_inv = prev_inv_by_agent.get(agent_key)
            is_new_user_turn = (not has_seen_agent) or (inv_id is None) or (prev_inv is None) or (inv_id != prev_inv)
            if is_new_user_turn:
                if agent_key not in first_input_by_agent:
                    first_input_by_agent[agent_key] = (fresh_tok + cached_tok)
                elif (fresh_tok + cached_tok) > first_input_by_agent[agent_key]:
                    history_grew = True
            if (
                is_new_user_turn
                and agent_key in prev_input_by_agent
                and fresh_tok > prev_input_by_agent[agent_key] + self.rules.compaction_growth_delta
            ):
                rapid_history_growth = True
            if is_new_user_turn:
                prev_input_by_agent[agent_key] = fresh_tok
                prev_inv_by_agent[agent_key] = inv_id

            cost_cell = f"≈${cost:<8.5f}" if (turn_price_est or t.get("tokens_estimated")) else f"${cost:<9.5f}"
            row = f"{idx:<6} | {query:<24} | {fresh_tok:<9,d} | {cached_tok:<9,d} | {output_tok:<8,d} | {think_tok:<7,d} | {cost_cell} | {status}"
            print(row)

        print("-" * 105)
        overall_prompt = total_fresh + total_cached
        overall_hit = (total_cached / overall_prompt * 100) if overall_prompt > 0 else 0.0
        summary_row = f"{'TOTAL':<6} | {f'{len(turns)} turns':<24} | {total_fresh:<9,d} | {total_cached:<9,d} | {total_output:<8,d} | {total_thinking:<7,d} | ${total_cost:<9.5f} | 📊 Avg Cache: {overall_hit:.1f}%"
        print(summary_row)
        print("=" * 105)

        if agent_rollup:
            print("\n🧭 SUB-AGENT ATTRIBUTION BREAKDOWN:")
            print("─" * 121)
            ag_header = f"{'Sub-Agent':<28} | {'Stage':<28} | {'Turns':<5} | {'Fresh In':<9} | {'Cached In':<9} | {'Output':<8} | {'Think':<7} | {'Cost ($)':<10} | {'Share'}"
            print(ag_header)
            print("─" * 121)
            for ag_name, ag_stats in sorted(agent_rollup.items(), key=lambda kv: kv[1]["cost"], reverse=True):
                share_pct = (ag_stats["cost"] / total_cost * 100.0) if total_cost > 0 else 0.0
                ag_stats["cost"] = round(ag_stats["cost"], 6)
                ag_stats["cost_share_pct"] = round(share_pct, 2)
                print(
                    f"{ag_name[:27]:<28} | {ag_stats['stage'][:27]:<28} | {ag_stats['turns']:<5,d} | "
                    f"{ag_stats['fresh']:<9,d} | {ag_stats['cached']:<9,d} | {ag_stats['output']:<8,d} | "
                    f"{ag_stats['thinking']:<7,d} | ${ag_stats['cost']:<9.5f} | {share_pct:5.1f}%"
                )
            print("─" * 121)
        self.last_agent_rollup = agent_rollup
        self.last_unregistered_models = unregistered_models
        self.last_pricing_estimated = bool(unregistered_models)

        # Exclude an agent's cold cache-warming first turn above min_cache_tokens when a subsequent turn on that agent hit cache
        for idx_u, tok_u, m_u, ag_key_u in uncached_candidates_raw:
            if ag_key_u in cached_seen_by_agent and first_large_idx_by_agent.get(ag_key_u) == idx_u:
                continue
            uncached_large_prompts.append((idx_u, tok_u, m_u))

        # Exclude lightweight candidate turns belonging to a sub-agent whose main turns in this session are heavy synthesis
        filtered_lightweight = [
            lw
            for lw in lightweight_turns
            if agent_max_output.get(str(lw.get("sub_agent") or ""), 0) <= self.rules.lightweight_turn_max_output_tokens
        ]

        max_per_session_user_turns = max(
            (len(session_seen_invs[sk]) + session_none_turns[sk] for sk in session_seen_invs),
            default=0,
        )
        effective_user_turns = max_per_session_user_turns

        recommendations = self.generate_recommendations(
            uncached_prompts=uncached_large_prompts,
            rapid_growth=rapid_history_growth,
            total_turns=effective_user_turns,
            total_cached=total_cached,
            total_cost=total_cost,
            total_fresh=total_fresh,
            total_output=total_output,
            total_thinking=total_thinking,
            heavy_thinking_turns=heavy_thinking_turns,
            lightweight_turns=filtered_lightweight,
            turns=turns,
            history_grew=history_grew,
        )
        self._print_optimization_recommendations(recommendations)
        if not recommendations and total_cached == 0 and 0 < max_prompt_tokens <= self.rules.min_cache_tokens:
            # Explains the apparent conflict with a `--min-cache-rate` budget gate on small-prompt agents.
            print(
                f" • ℹ️  Every prompt is under the {self.rules.min_cache_tokens:,}-token context-caching threshold "
                f"(largest: {max_prompt_tokens:,}), so a 0% cache-hit rate is expected here. Leave "
                "`--min-cache-rate` out of budget gates until the static prompt prefix grows past it.\n"
            )
        return recommendations

    def _estimate_fresh_token_savings(
        self,
        ratio: float,
        total_fresh: int,
        turns: Optional[List[Dict[str, Any]]],
    ) -> float:
        default_model = str(self.pricing.config.default_model or "publishers/google/models/gemini-3.8-flash")
        if turns:
            savings = 0.0
            for t in turns:
                if not isinstance(t, dict):
                    continue
                norm = normalize_turn_tokens(t)
                t_fresh = norm["fresh_tokens"]
                t_model = str(t.get("model_name") or t.get("model") or default_model)
                in_rate = float(self.pricing.get_rate(t_model).get("input", 0.0))
                savings += (t_fresh * ratio / 1_000_000.0) * in_rate
            return savings
        in_rate = float(self.pricing.get_rate(default_model).get("input", 0.0))
        return (total_fresh * ratio / 1_000_000.0) * in_rate

    def generate_recommendations(
        self,
        uncached_prompts: List[Any],
        rapid_growth: bool,
        total_turns: int,
        total_cached: int,
        total_cost: float,
        total_fresh: int,
        total_output: int = 0,
        total_thinking: int = 0,
        heavy_thinking_turns: Optional[List[Any]] = None,
        lightweight_turns: Optional[List[Any]] = None,
        turns: Optional[List[Dict[str, Any]]] = None,
        history_grew: Optional[bool] = None,
    ) -> List[str]:
        """Evaluates configured heuristics and custom callbacks to produce formatted optimization recommendations."""
        recs: List[str] = []
        default_model = self.pricing.config.default_model

        app_finops_cfg: Dict[str, Any] = {}
        skills_already_active = False
        max_turn_fresh = 0
        if turns:
            for t in turns:
                if not isinstance(t, Mapping):
                    continue
                t_norm = normalize_turn_tokens(t)
                max_turn_fresh = max(max_turn_fresh, t_norm["fresh_tokens"])
                if t.get("skills_active"):
                    skills_already_active = True
                for tc in (t.get("tools_called") or []):
                    if "load_skill" in str(tc).lower():
                        skills_already_active = True
                rm = t.get("raw_json")
                if isinstance(rm, Mapping):
                    af = rm.get("app_finops_config")
                    if isinstance(af, Mapping):
                        app_finops_cfg.update(af)
                        if af.get("skill_toolset_configured"):
                            skills_already_active = True

        cache_cfg_active = bool(app_finops_cfg.get("context_cache_configured"))
        comp_cfg_active = bool(app_finops_cfg.get("events_compaction_configured"))
        comp_cfg_interval = app_finops_cfg.get("compaction_interval")
        eff_comp_interval = (
            int(comp_cfg_interval)
            if isinstance(comp_cfg_interval, int) and comp_cfg_interval >= 1
            else self.rules.compaction_interval
        )

        if uncached_prompts and not (cache_cfg_active and total_cached > 0):
            normalized_uncached = []
            total_cache_savings = 0.0
            discounts = []
            for item in uncached_prompts:
                if len(item) >= 3:
                    turn_no, tok, m_name = item[0], item[1], item[2]
                else:
                    turn_no, tok, m_name = item[0], item[1], default_model
                normalized_uncached.append((turn_no, tok))
                rates = self.pricing.get_rate(m_name)
                in_r = float(rates.get("input", 0.0))
                cac_r = float(rates.get("cached", 0.0))
                total_cache_savings += max(0.0, (tok / 1_000_000.0) * (in_r - cac_r))
                discounts.append(self.pricing.get_cache_discount_pct(m_name))

            turns_str = ", ".join(f"Turn #{turn} ({tok:,} tokens)" for turn, tok in normalized_uncached)
            avg_discount_pct = (sum(discounts) / len(discounts)) if discounts else self.pricing.get_cache_discount_pct(default_model)
            if cache_cfg_active and total_cached == 0 and self.prompts.get("context_caching_configured_no_hits"):
                recs.append(
                    self.prompts["context_caching_configured_no_hits"].format(
                        turns_str=turns_str,
                        min_cache_tokens=self.rules.min_cache_tokens,
                        cache_ttl_seconds=self.rules.cache_ttl_seconds,
                        cache_discount_pct=avg_discount_pct,
                        estimated_cache_savings=total_cache_savings,
                    )
                )
            elif self.prompts.get("context_caching"):
                recs.append(
                    self.prompts["context_caching"].format(
                        turns_str=turns_str,
                        min_cache_tokens=self.rules.min_cache_tokens,
                        cache_ttl_seconds=self.rules.cache_ttl_seconds,
                        cache_discount_pct=avg_discount_pct,
                        estimated_cache_savings=total_cache_savings,
                    )
                )

        effective_history_grew = True if history_grew is None else bool(history_grew)
        if comp_cfg_active:
            if rapid_growth and total_turns > eff_comp_interval and self.prompts.get("history_compaction_configured_growth"):
                est_compaction_savings = self._estimate_fresh_token_savings(0.35, total_fresh, turns)
                compaction_msg = self.prompts["history_compaction_configured_growth"].format(
                    total_turns=total_turns,
                    compaction_interval=eff_comp_interval,
                    compaction_overlap=self.rules.compaction_overlap,
                )
                if "est. savings:" not in compaction_msg and est_compaction_savings > 0:
                    compaction_msg = f"{compaction_msg} (est. savings: ${est_compaction_savings:.5f}/session)."
                recs.append(compaction_msg)
        elif (
            rapid_growth
            or (total_turns >= self.rules.compaction_turn_threshold and effective_history_grew)
        ) and self.prompts.get("history_compaction"):
            est_compaction_savings = self._estimate_fresh_token_savings(0.35, total_fresh, turns)
            compaction_msg = self.prompts["history_compaction"].format(
                total_turns=total_turns,
                compaction_interval=self.rules.compaction_interval,
                compaction_overlap=self.rules.compaction_overlap,
            )
            if "est. savings:" not in compaction_msg and est_compaction_savings > 0:
                compaction_msg = f"{compaction_msg} (est. savings: ${est_compaction_savings:.5f}/session)."
            recs.append(compaction_msg)

        should_flag_modular_skills = (
            (not skills_already_active and max_turn_fresh > self.rules.modular_skills_token_threshold)
            if turns is not None
            else (total_fresh > self.rules.modular_skills_token_threshold)
        )
        if should_flag_modular_skills and self.prompts.get("modular_skills"):
            est_skills_savings = self._estimate_fresh_token_savings(
                self.rules.modular_skills_reduction_ratio,
                total_fresh,
                turns,
            )
            recs.append(
                self.prompts["modular_skills"].format(
                    total_fresh=total_fresh,
                    modular_skills_token_threshold=self.rules.modular_skills_token_threshold,
                    estimated_skills_savings=est_skills_savings,
                )
            )

        thinking_ratio = (total_thinking / total_output) if total_output > 0 else 0.0
        thinking_ratio_str = (
            f"{thinking_ratio:.2f}x"
            if total_output > 0
            else ("∞ (0 visible output)" if total_thinking > 0 else "0.00x")
        )
        session_heavy_thinking = (
            total_thinking > self.rules.recommended_thinking_budget
            and (
                total_output == 0
                or thinking_ratio > self.rules.max_thinking_to_output_ratio
            )
        )
        if (session_heavy_thinking or heavy_thinking_turns) and self.prompts.get("thinking_budget"):
            heavy_agents_suffix = ""
            est_thinking_savings = 0.0
            if heavy_thinking_turns:
                labels = []
                for item in heavy_thinking_turns:
                    lbl, thk = item[1], int(item[2])
                    out_t = int(item[3]) if len(item) >= 4 else 0
                    m_name = item[4] if len(item) >= 5 else default_model
                    if len(labels) < 3:
                        turn_ratio_str = f", {thk / out_t:.2f}x" if out_t > 0 else ", ∞ (0 visible output)"
                        labels.append(f"{lbl} ({thk:,} think{turn_ratio_str})")
                    excess_thk = max(0, thk - self.rules.recommended_thinking_budget)
                    out_r = float(self.pricing.get_rate(m_name).get("output", 0.0))
                    est_thinking_savings += (excess_thk / 1_000_000.0) * out_r
                heavy_agents_suffix = f" in {', '.join(labels)}"
            else:
                session_model = (turns[0].get("model_name") if turns else None) or default_model
                excess_thk = max(0, total_thinking - self.rules.recommended_thinking_budget)
                out_r = float(self.pricing.get_rate(session_model).get("output", 0.0))
                est_thinking_savings = (excess_thk / 1_000_000.0) * out_r

            tmpl = self.prompts["thinking_budget"]
            think_msg = tmpl.format(
                total_thinking=total_thinking,
                total_output=total_output,
                thinking_ratio=thinking_ratio,
                thinking_ratio_str=thinking_ratio_str,
                heavy_agents_suffix=heavy_agents_suffix,
                recommended_thinking_budget=self.rules.recommended_thinking_budget,
            )
            if total_output == 0 and total_thinking > 0 and "{thinking_ratio_str}" not in tmpl:
                think_msg = think_msg.replace("0.00x", "∞ (0 visible output)").replace("infx", "∞ (0 visible output)")
            if "est. savings:" not in think_msg and est_thinking_savings > 0:
                think_msg = f"{think_msg} (est. savings: ${est_thinking_savings:.5f})."
            recs.append(think_msg)

        if self.rules.enable_model_tiering_hint and lightweight_turns and self.prompts.get("model_tiering"):
            first_lw = lightweight_turns[0]
            tierable_labels = []
            distinct_target_models: List[str] = []
            current_lw_cost = 0.0
            projected_lw_cost = 0.0

            for lw in lightweight_turns:
                if isinstance(lw, dict):
                    turn_model = lw.get("model_name") or default_model
                    if self.rules.recommended_lite_model:
                        turn_target_lite = self.rules.recommended_lite_model
                    else:
                        turn_provider = self.pricing.get_rate(turn_model).get("provider", "google")
                        turn_target_lite = self.pricing.get_cheapest_model(provider=turn_provider)["short_id"]
                    cur_cost = float(lw["cost"])
                    proj_cost = self.pricing.calculate_cost(
                        turn_target_lite,
                        lw["fresh_tok"],
                        lw["cached_tok"],
                        lw["output_tok"],
                        lw.get("think_tok", 0),
                    )
                    if proj_cost < cur_cost:
                        tierable_labels.append(lw["label"])
                        current_lw_cost += cur_cost
                        projected_lw_cost += proj_cost
                        if turn_target_lite not in distinct_target_models:
                            distinct_target_models.append(turn_target_lite)
                else:
                    tierable_labels.append(str(lw))

            if not distinct_target_models:
                fallback_prov = self.pricing.get_rate(default_model).get("provider", "google")
                fallback_lite = self.rules.recommended_lite_model or self.pricing.get_cheapest_model(provider=fallback_prov)["short_id"]
                distinct_target_models.append(fallback_lite)

            primary_lite_model = distinct_target_models[0]
            recommended_lite_display = " / ".join(distinct_target_models)
            lite_rates = self.pricing.get_rate(primary_lite_model)
            lite_in_rate = float(lite_rates.get("input", 0.0))
            lite_out_rate = float(lite_rates.get("output", 0.0))

            tiering_savings_dollars = max(0.0, current_lw_cost - projected_lw_cost)
            tiering_savings_pct = (tiering_savings_dollars / current_lw_cost * 100.0) if current_lw_cost > 0 else 0.0

            if tierable_labels and (tiering_savings_dollars > 0.0 or not isinstance(first_lw, dict)):
                displayed_labels = ", ".join(tierable_labels[:3])
                if len(tierable_labels) > 3:
                    displayed_labels += f" (+{len(tierable_labels) - 3} more)"
                recs.append(
                    self.prompts["model_tiering"].format(
                        lightweight_turns_str=displayed_labels,
                        recommended_lite_model=recommended_lite_display,
                        lite_input_rate=lite_in_rate,
                        lite_output_rate=lite_out_rate,
                        tiering_savings_pct=tiering_savings_pct,
                        tiering_savings_dollars=tiering_savings_dollars,
                    )
                )

        context_payload = {
            "total_turns": total_turns,
            "total_fresh": total_fresh,
            "total_cached": total_cached,
            "total_output": total_output,
            "total_thinking": total_thinking,
            "total_cost": total_cost,
            "thinking_ratio": thinking_ratio,
            "uncached_prompts": uncached_prompts,
            "turns": turns or [],
        }
        for cb in self.custom_callbacks:
            try:
                custom_rec = cb(context_payload)
                if custom_rec:
                    recs.append(str(custom_rec))
            except Exception:
                pass

        return recs

    def _print_optimization_recommendations(self, recommendations: List[str]):
        print("\n💡 AUTOMATED ADK OPTIMIZATION RECOMMENDATIONS:")
        print("─" * 60)
        if recommendations:
            for rec in recommendations:
                print(f" • {rec}\n")
        else:
            well_opt = self.prompts.get(
                "well_optimized",
                "✨ No optimization opportunities detected: none of the 5 FinOps rules fired for this session.",
            )
            print(f" • {well_opt}\n")


def _unwrap_json_value(val: Any, max_depth: int = 3) -> Any:
    curr = val
    depth = 0
    while isinstance(curr, str) and depth < max_depth:
        stripped = curr.strip()
        if not stripped or stripped[0] not in ("{", "[", '"'):
            break
        try:
            parsed = json.loads(stripped)
        except Exception:
            break
        if parsed == curr:
            break
        curr = parsed
        depth += 1
    return curr


def _unwrap_json_dict(val: Any) -> Dict[str, Any]:
    unwrapped = _unwrap_json_value(val)
    return dict(unwrapped) if isinstance(unwrapped, Mapping) else {}


def _first_present_int(mapping: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key in mapping and mapping[key] is not None and not isinstance(mapping[key], bool):
            return _safe_nonneg_int(mapping[key])
    return None


_KNOWN_USAGE_KEYS = (
    "prompt_token_count",
    "promptTokens",
    "prompt_tokens",
    "input_tokens",
    "prompt",
    "tool_use_prompt_token_count",
    "toolUsePromptTokenCount",
    "usage_tool_use_tokens",
    "tool_use_tokens",
    "cached_content_token_count",
    "cachedContentTokenCount",
    "cacheReadTokens",
    "cached_tokens",
    "candidates_token_count",
    "candidatesTokenCount",
    "candidate_tokens",
    "completion_tokens",
    "output_tokens",
    "completion",
    "thoughts_token_count",
    "thoughtsTokenCount",
    "thinkingOutputTokens",
    "reasoning_tokens",
    "thinking_tokens",
)


def _extract_usage_counts(usage: Mapping[str, Any]) -> Tuple[int, int, int, int, bool]:
    has_keys = any(usage.get(k) is not None and not isinstance(usage.get(k), bool) for k in _KNOWN_USAGE_KEYS)
    base_p = _first_present_int(
        usage,
        "prompt_token_count",
        "promptTokens",
        "prompt_tokens",
        "prompt",
    )
    tool_p = (
        _first_present_int(
            usage,
            "tool_use_prompt_token_count",
            "toolUsePromptTokenCount",
            "usage_tool_use_tokens",
            "tool_use_tokens",
        )
        or 0
    )
    cac = (
        _first_present_int(
            usage,
            "cached_content_token_count",
            "cachedContentTokenCount",
            "cacheReadTokens",
            "cached_tokens",
        )
        or 0
    )
    if base_p is not None:
        raw_p = base_p + tool_p
    else:
        in_only = _first_present_int(usage, "input_tokens")
        if in_only is not None:
            # When only input_tokens is supplied alongside cached_tokens, treat input_tokens as fresh
            raw_p = in_only + cac + tool_p if ("cached_tokens" in usage and "prompt_tokens" not in usage) else (in_only + tool_p)
        else:
            raw_p = tool_p

    out = (
        _first_present_int(
            usage,
            "candidates_token_count",
            "candidatesTokenCount",
            "candidate_tokens",
            "completion_tokens",
            "output_tokens",
            "completion",
        )
        or 0
    )
    thk = (
        _first_present_int(
            usage,
            "thoughts_token_count",
            "thoughtsTokenCount",
            "thinkingOutputTokens",
            "reasoning_tokens",
            "thinking_tokens",
        )
        or 0
    )
    return raw_p, cac, out, thk, has_keys


def _extract_eval_invocation_latency_ms(actual: Mapping[str, Any]) -> int:
    for ms_key in (
        "model_duration_ms",
        "modelDurationMs",
        "latency_ms",
        "latencyMs",
        "invocation_duration_ms",
        "invocationDurationMs",
    ):
        raw_ms = actual.get(ms_key)
        if raw_ms is not None and not isinstance(raw_ms, bool):
            try:
                f_ms = float(raw_ms)
                if f_ms > 0.0:
                    return max(1, int(round(f_ms)))
            except (ValueError, TypeError):
                continue

    for sec_key in (
        "model_duration",
        "modelDuration",
        "duration",
        "invocation_duration",
        "invocationDuration",
    ):
        raw_sec = actual.get(sec_key)
        if raw_sec is not None and not isinstance(raw_sec, bool):
            try:
                f_sec = float(raw_sec)
                if f_sec > 0.0:
                    return max(1, int(round(f_sec * 1000.0)))
            except (ValueError, TypeError):
                continue
    return 0


def _extract_skill_from_tool(tool_name: str, tool_args: Any) -> Optional[str]:
    t_low = str(tool_name or "").strip().lower()
    if not t_low or "skill" not in t_low or "unload" in t_low:
        return None
    args_dict = _unwrap_json_dict(tool_args)
    raw_skill = (
        args_dict.get("skill_name")
        or args_dict.get("skillName")
        or args_dict.get("name")
        or args_dict.get("skill")
    )
    if raw_skill is not None and str(raw_skill).strip():
        return str(raw_skill).strip()
    return None


def _extract_function_calls_from_parts(parts: Any) -> Tuple[List[str], List[str]]:
    tools: List[str] = []
    skills: List[str] = []
    if not isinstance(parts, list):
        return tools, skills
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        fc = part.get("function_call") or part.get("functionCall")
        if isinstance(fc, Mapping):
            t_name = str(fc.get("name") or fc.get("tool") or "").strip()
            t_args = fc.get("args") if fc.get("args") is not None else fc.get("arguments")
            if t_name and t_name not in tools:
                tools.append(t_name)
            sk = _extract_skill_from_tool(t_name, t_args)
            if sk and sk not in skills:
                skills.append(sk)
    return tools, skills


def _normalize_single_turn_dict(turn_dict: Dict[str, Any], top_session_id: Optional[str] = None) -> Dict[str, Any]:
    out = dict(turn_dict)
    if top_session_id and not out.get("session_id"):
        out["session_id"] = str(top_session_id)
    um = out.get("usage_metadata") or out.get("usageMetadata")
    if isinstance(um, Mapping):
        tool_extra = (
            _first_present_int(
                um,
                "tool_use_prompt_token_count",
                "toolUsePromptTokenCount",
                "usage_tool_use_tokens",
                "tool_use_tokens",
            )
            or 0
        )
        if "input_tokens" not in out and "prompt_tokens" not in out:
            raw_p, cac, cand_out, thk, _ = _extract_usage_counts(um)
            out["prompt_tokens"] = raw_p
            out["input_tokens"] = max(0, raw_p - cac)
            out.setdefault("cached_tokens", cac)
            out.setdefault("output_tokens", cand_out)
            out.setdefault("thinking_tokens", thk)
        elif tool_extra > 0 and "input_tokens" not in out and "prompt_tokens" in out:
            out["prompt_tokens"] = _safe_nonneg_int(out.get("prompt_tokens")) + tool_extra
    elif out.get("tool_use_prompt_token_count") or out.get("toolUsePromptTokenCount") or out.get("usage_tool_use_tokens"):
        tool_top = _safe_nonneg_int(
            out.get("tool_use_prompt_token_count")
            or out.get("toolUsePromptTokenCount")
            or out.get("usage_tool_use_tokens")
        )
        if tool_top > 0 and "input_tokens" not in out:
            if out.get("prompt_tokens") is not None:
                out["prompt_tokens"] = _safe_nonneg_int(out.get("prompt_tokens")) + tool_top
            elif out.get("usage_prompt_tokens") is not None:
                out["prompt_tokens"] = _safe_nonneg_int(out.get("usage_prompt_tokens")) + tool_top
    return out


_NON_TRACE_PROJECT_JSON_FILES = frozenset({
    "test_config.json",
    "deployment_metadata.json",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "biome.json",
    "devcontainer.json",
    "models_config.json",
})


def _is_non_result_adk_config_file(file_path: str) -> bool:
    base = os.path.basename(file_path).lower()
    return base.endswith(".evalset.json") or base in _NON_TRACE_PROJECT_JSON_FILES


def is_uninstrumented_eval_cases_payload(data: Any) -> bool:
    """Returns True when `data` is an EvaluationDataset (`eval_cases`) with zero token usage metadata."""
    unwrapped = _unwrap_json_value(data)
    if not isinstance(unwrapped, Mapping):
        return False
    if any(k in unwrapped for k in ("turns", "eval_case_results", "evalCaseResults", "events")):
        return False
    raw_cases = unwrapped.get("eval_cases") if "eval_cases" in unwrapped else unwrapped.get("evalCases")
    if not isinstance(raw_cases, list):
        return False
    for case in raw_cases:
        if not isinstance(case, Mapping):
            continue
        usage = _unwrap_json_dict(
            case.get("token_usage_details")
            or case.get("tokenUsageDetails")
            or case.get("usage_metadata")
            or case.get("usageMetadata")
            or case.get("usage")
        )
        _, _, _, _, has_usage = _extract_usage_counts(usage)
        if has_usage:
            return False
    return True


def split_coexisting_uninstrumented_eval_files(file_paths: List[str]) -> Tuple[List[str], List[str]]:
    """
    When `agents-cli eval generate` writes Stage-2 `traces_<ts>.json` (`eval_cases` without token usage)
    into the same `artifacts/traces/` folder as `ADKTokenomicsPlugin` session traces, skip the
    uninstrumented `eval_cases` files ONLY when at least one sibling file carries real instrumented turns.

    Returns `(kept, skipped)` so callers can tell users which files were left out and why.
    """
    if len(file_paths) <= 1:
        return list(file_paths), []
    uninstrumented: set[str] = set()
    has_instrumented_turns = False
    for fp in file_paths:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        if is_uninstrumented_eval_cases_payload(payload):
            uninstrumented.add(fp)
            continue
        try:
            real_turns = parse_trace_or_eval_payload(payload, allow_estimated_tokens=False)
            if real_turns:
                has_instrumented_turns = True
        except Exception:
            continue
    if uninstrumented and has_instrumented_turns:
        return [fp for fp in file_paths if fp not in uninstrumented], [fp for fp in file_paths if fp in uninstrumented]
    return list(file_paths), []


def filter_coexisting_uninstrumented_eval_files(file_paths: List[str]) -> List[str]:
    """Backward-compatible wrapper returning only the kept files (see `split_coexisting_uninstrumented_eval_files`)."""
    return split_coexisting_uninstrumented_eval_files(file_paths)[0]


SKIPPED_EVAL_FILE_REASON = (
    "eval file without token usage metadata (e.g. `agents-cli eval generate` traces_<ts>.json); "
    "skipped because instrumented plugin session traces are present"
)


def _collect_directory_trace_candidates(dir_path: str) -> List[str]:
    direct = [
        p
        for p in sorted(glob.glob(os.path.join(dir_path, "*.json")))
        if not _is_non_result_adk_config_file(p)
    ]
    nested_traces_dir = os.path.join(dir_path, "artifacts", "traces")
    nested_traces = (
        [
            p
            for p in sorted(glob.glob(os.path.join(nested_traces_dir, "*.json")))
            if not _is_non_result_adk_config_file(p)
        ]
        if os.path.isdir(nested_traces_dir)
        else []
    )
    nested_eval_dir = os.path.join(dir_path, ".adk", "eval_history")
    nested_eval = (
        sorted(glob.glob(os.path.join(nested_eval_dir, "*.evalset_result.json")))
        if os.path.isdir(nested_eval_dir)
        else []
    )
    seen_abs: set[str] = set()
    ordered: List[str] = []
    for candidate in direct + nested_traces + nested_eval:
        ap = os.path.abspath(candidate)
        if ap not in seen_abs:
            seen_abs.add(ap)
            ordered.append(candidate)
    return ordered


def discover_directory_trace_files_with_skipped(dir_path: str) -> Tuple[List[str], List[str]]:
    """Like `discover_directory_trace_files`, but also returns the eval files that were skipped."""
    return split_coexisting_uninstrumented_eval_files(_collect_directory_trace_candidates(dir_path))


def discover_directory_trace_files(dir_path: str) -> List[str]:
    """Discovers trace and eval result JSON files in a trace folder or project root (e.g. `--traces .`)."""
    return discover_directory_trace_files_with_skipped(dir_path)[0]


def parse_trace_or_eval_payload(
    data: Any,
    default_model: str = "publishers/google/models/gemini-3.8-flash",
    allow_estimated_tokens: bool = True,
) -> List[Dict[str, Any]]:
    """
    Canonical parser for plugin session traces (`turns`), native ADK EvalSetResult files
    (`eval_case_results` / `evalCaseResults`, including double-encoded JSON strings),
    and legacy `eval_cases` / `events` JSON structures. Raises ValueError if schema is unrecognized
    or when uninstrumented `eval_cases` require estimated tokens under `allow_estimated_tokens=False`.
    """
    unwrap_depth = 0
    while isinstance(data, str) and unwrap_depth < 3:
        stripped = data.strip()
        if not stripped:
            raise ValueError("Trace payload string is empty.")
        try:
            data = json.loads(stripped)
        except Exception as exc:
            raise ValueError(f"Failed to decode stringified JSON trace payload: {exc}") from exc
        unwrap_depth += 1

    turns: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            item_unwrapped = _unwrap_json_value(item)
            if isinstance(item_unwrapped, dict):
                turns.append(_normalize_single_turn_dict(item_unwrapped))
        return turns

    if not isinstance(data, dict):
        raise ValueError("Trace payload must be a JSON object or array of turns.")

    if "turns" in data and isinstance(data["turns"], list):
        top_sid = str(data["session_id"]).strip() if data.get("session_id") else None
        return [
            _normalize_single_turn_dict(t, top_session_id=top_sid)
            for t in (_unwrap_json_value(item) for item in data["turns"])
            if isinstance(t, dict)
        ]

    raw_case_results = data.get("eval_case_results") if "eval_case_results" in data else data.get("evalCaseResults")
    if isinstance(raw_case_results, list):
        saw_invocations_without_usage = False
        for case_idx, raw_case_res in enumerate(raw_case_results, 1):
            case_res = _unwrap_json_value(raw_case_res)
            if not isinstance(case_res, dict):
                continue
            eval_id = str(
                case_res.get("eval_id")
                or case_res.get("evalId")
                or case_res.get("eval_case_id")
                or case_res.get("evalCaseId")
                or f"eval_case_{case_idx}"
            )
            sess_details = _unwrap_json_dict(case_res.get("session_details") or case_res.get("sessionDetails"))
            sess_id = str(
                case_res.get("session_id")
                or case_res.get("sessionId")
                or sess_details.get("id")
                or f"adk_eval_{eval_id}"
            )
            inv_list = (
                case_res.get("eval_metric_result_per_invocation")
                or case_res.get("evalMetricResultPerInvocation")
                or case_res.get("invocations")
                or []
            )
            for inv_idx, raw_inv_entry in enumerate(inv_list, 1):
                inv_entry = _unwrap_json_value(raw_inv_entry)
                if not isinstance(inv_entry, dict):
                    continue
                actual_raw = inv_entry.get("actual_invocation") or inv_entry.get("actualInvocation") or inv_entry
                actual = _unwrap_json_value(actual_raw)
                if not isinstance(actual, dict):
                    continue
                inv_id = str(
                    actual.get("invocation_id")
                    or actual.get("invocationId")
                    or f"{eval_id}_inv_{inv_idx}"
                )
                user_content = _unwrap_json_dict(actual.get("user_content") or actual.get("userContent"))
                user_text = "".join(
                    str(p.get("text", ""))
                    for p in (user_content.get("parts") or [])
                    if isinstance(p, Mapping)
                ).strip() or eval_id
                inter = _unwrap_json_dict(actual.get("intermediate_data") or actual.get("intermediateData"))
                inv_events = inter.get("invocation_events") if "invocation_events" in inter else inter.get("invocationEvents")
                inv_lat = _extract_eval_invocation_latency_ms(actual)

                if isinstance(inv_events, list) and inv_events:
                    normalized_events = [
                        _unwrap_json_value(ev)
                        for ev in inv_events
                        if isinstance(_unwrap_json_value(ev), dict)
                    ]
                    usage_events = [
                        ev
                        for ev in normalized_events
                        if isinstance(ev.get("usage_metadata") or ev.get("usageMetadata") or ev.get("usage"), Mapping)
                    ]
                    if usage_events:
                        num_usage_events = len(usage_events)
                        base_lat, rem_lat = divmod(max(0, inv_lat), num_usage_events) if num_usage_events > 0 else (0, 0)

                        for ev_idx, ev in enumerate(usage_events):
                            usage = _unwrap_json_dict(ev.get("usage_metadata") or ev.get("usageMetadata") or ev.get("usage"))
                            raw_p, cac, out, thk, _ = _extract_usage_counts(usage)
                            author = str(ev.get("author") or "agent")
                            ev_content = _unwrap_json_dict(ev.get("content"))
                            ev_parts = ev_content.get("parts") or []
                            resp_text = "".join(
                                str(p.get("text", ""))
                                for p in ev_parts
                                if isinstance(p, Mapping)
                            ).strip()
                            ev_tools, ev_skills = _extract_function_calls_from_parts(ev_parts)

                            explicit_ev_lat = ev.get("latency_ms") if ev.get("latency_ms") is not None else ev.get("latencyMs")
                            explicit_ev_dur = ev.get("model_duration") if ev.get("model_duration") is not None else ev.get("modelDuration")
                            if explicit_ev_lat is not None and not isinstance(explicit_ev_lat, bool):
                                f_ev_lat = float(explicit_ev_lat)
                                resolved_ev_lat = max(1, int(round(f_ev_lat))) if f_ev_lat > 0 else 0
                            elif explicit_ev_dur is not None and not isinstance(explicit_ev_dur, bool) and float(explicit_ev_dur) > 0:
                                resolved_ev_lat = max(1, int(round(float(explicit_ev_dur) * 1000.0)))
                            elif inv_lat > 0:
                                resolved_ev_lat = max(1, base_lat + (1 if ev_idx < rem_lat else 0))
                            else:
                                resolved_ev_lat = 0

                            turn_entry: Dict[str, Any] = {
                                "session_id": sess_id,
                                "invocation_id": inv_id,
                                "sub_agent": author,
                                "user_query": f"[{author}] {user_text}" if author and author != "user" else user_text,
                                "agent_response": resp_text,
                                "prompt_tokens": raw_p,
                                "input_tokens": max(0, raw_p - cac),
                                "cached_tokens": cac,
                                "output_tokens": out,
                                "thinking_tokens": thk,
                                "latency_ms": resolved_ev_lat,
                                "model_name": str(
                                    ev.get("model_version")
                                    or ev.get("modelVersion")
                                    or ev.get("model")
                                    or actual.get("model_version")
                                    or actual.get("modelVersion")
                                    or default_model
                                ),
                            }
                            if ev_tools:
                                turn_entry["tools_called"] = ev_tools
                            if ev_skills:
                                turn_entry["skills_active"] = ev_skills
                            turns.append(turn_entry)
                        continue

                usage = _unwrap_json_dict(
                    actual.get("usage_metadata")
                    or actual.get("usageMetadata")
                    or actual.get("token_usage_details")
                    or actual.get("tokenUsageDetails")
                    or actual.get("usage")
                )
                raw_p, cac, out, thk, has_usage = _extract_usage_counts(usage)
                if has_usage:
                    turns.append({
                        "session_id": sess_id,
                        "invocation_id": inv_id,
                        "user_query": user_text,
                        "prompt_tokens": raw_p,
                        "input_tokens": max(0, raw_p - cac),
                        "cached_tokens": cac,
                        "output_tokens": out,
                        "thinking_tokens": thk,
                        "latency_ms": inv_lat,
                        "model_name": str(
                            actual.get("model_version")
                            or actual.get("modelVersion")
                            or actual.get("model")
                            or default_model
                        ),
                    })
                else:
                    saw_invocations_without_usage = True

        if not turns and saw_invocations_without_usage and not allow_estimated_tokens:
            raise ValueError(
                "ADK EvalSetResult artifact contains invocations without token usage_metadata; "
                "ensure ADK eval telemetry or ADKTokenomicsPlugin is enabled."
            )
        return turns

    raw_eval_cases = data.get("eval_cases") if "eval_cases" in data else data.get("evalCases")
    if isinstance(raw_eval_cases, list):
        for case in raw_eval_cases:
            if not isinstance(case, dict):
                continue
            prompt_text = ""
            p = case.get("prompt") or {}
            if isinstance(p, Mapping):
                for part in p.get("parts") or []:
                    if isinstance(part, Mapping):
                        prompt_text += str(part.get("text", ""))

            usage = _unwrap_json_dict(
                case.get("token_usage_details")
                or case.get("tokenUsageDetails")
                or case.get("usage_metadata")
                or case.get("usageMetadata")
                or case.get("usage")
            )
            raw_p, cac, out, thk, has_usage = _extract_usage_counts(usage)
            if has_usage:
                turns.append({
                    "user_query": prompt_text.strip() or case.get("eval_case_id") or case.get("evalId") or "Eval Case",
                    "prompt_tokens": raw_p,
                    "input_tokens": max(0, raw_p - cac),
                    "cached_tokens": cac,
                    "output_tokens": out,
                    "thinking_tokens": thk,
                    "latency_ms": _extract_eval_invocation_latency_ms(case),
                    "model_name": case.get("model_name") or case.get("model_version") or case.get("model") or default_model,
                })
            else:
                if not allow_estimated_tokens:
                    raise ValueError(
                        "Legacy 'eval_cases' artifact lacks token usage metadata; "
                        "estimated character-heuristic tokens are disallowed in strict assertion mode."
                    )
                resp_text = ""
                for r in case.get("responses") or []:
                    if isinstance(r, Mapping):
                        resp_obj = r.get("response") or {}
                        if isinstance(resp_obj, Mapping):
                            for part in resp_obj.get("parts") or []:
                                if isinstance(part, Mapping):
                                    resp_text += str(part.get("text", ""))

                instruction_len = 0
                agent_data = (case.get("agent_data") or {}).get("agents") or {}
                if isinstance(agent_data, Mapping):
                    for _, a_info in agent_data.items():
                        if isinstance(a_info, Mapping):
                            instruction_len += len(str(a_info.get("instruction", "")))

                est_prompt_tokens = max(10, (len(prompt_text) + instruction_len) // 4)
                est_output_tokens = max(5, len(resp_text) // 4)

                turns.append({
                    "user_query": prompt_text.strip() or case.get("eval_case_id") or case.get("evalId") or "Eval Case",
                    "input_tokens": est_prompt_tokens,
                    "cached_tokens": 0,
                    "output_tokens": est_output_tokens,
                    "thinking_tokens": 0,
                    "model_name": case.get("model_name") or default_model,
                    "tokens_estimated": True,
                })
        return turns

    if "events" in data and isinstance(data["events"], list):
        for ev in data["events"]:
            if not isinstance(ev, dict):
                continue
            usage = _unwrap_json_dict(ev.get("usage_metadata") or ev.get("usageMetadata") or ev.get("usage"))
            raw_p, cac, out, thk, has_usage = _extract_usage_counts(usage)
            if not has_usage and not allow_estimated_tokens:
                raise ValueError("Event entry in 'events' artifact lacks token usage_metadata.")
            turns.append({
                "user_query": ev.get("user_query") or ev.get("query") or "Turn",
                "prompt_tokens": raw_p,
                "input_tokens": max(0, raw_p - cac),
                "cached_tokens": cac,
                "output_tokens": out,
                "thinking_tokens": thk,
                "latency_ms": _extract_eval_invocation_latency_ms(ev),
                "model_name": ev.get("model_name") or ev.get("model_version") or default_model,
            })
        return turns

    if any(
        k in data
        for k in (
            "input_tokens",
            "prompt_tokens",
            "output_tokens",
            "completion_tokens",
            "cached_tokens",
            "usage_metadata",
            "usageMetadata",
        )
    ):
        return [_normalize_single_turn_dict(data)]

    raise ValueError(f"Unrecognized trace or eval JSON schema (keys: {list(data.keys())[:6]}).")


def _emit_profile_error(msg: str, output_format: str = "text") -> None:
    if output_format == "json":
        print(json.dumps({"error": msg}))
    else:
        print(f"[ERROR] {msg}")


def _build_profiler_config(
    prompts_config_path: Optional[str],
    rules_config: Optional[OptimizationRulesConfig],
    optimization_prompts: Optional[Dict[str, str]],
    cli_rule_overrides: Optional[Dict[str, Any]] = None,
) -> TokenomicsConfig:
    """
    Loads TokenomicsConfig (strictly validating prompts_config_path if given) and ensures
    explicit CLI/caller rule overrides take precedence over rules loaded from --prompts-file.
    """
    if prompts_config_path is not None:
        cleaned_pf = str(prompts_config_path).strip()
        if not cleaned_pf or not os.path.isfile(cleaned_pf):
            raise FileNotFoundError(f"Prompts config file not found: {prompts_config_path}")
        try:
            with open(cleaned_pf, "r", encoding="utf-8") as pf:
                loaded_pf = json.load(pf)
        except Exception as exc:
            raise ValueError(f"Failed to parse --prompts-file '{prompts_config_path}': {exc}") from exc
        if not isinstance(loaded_pf, dict):
            raise ValueError(
                f"Prompts config file '{prompts_config_path}' must contain a JSON object (dict), got {type(loaded_pf).__name__}."
            )
        if "prompts" in loaded_pf and not isinstance(loaded_pf["prompts"], dict):
            raise ValueError(f"'prompts' section in '{prompts_config_path}' must be a JSON object.")
        if "rules" in loaded_pf and not isinstance(loaded_pf["rules"], dict):
            raise ValueError(f"'rules' section in '{prompts_config_path}' must be a JSON object.")

    default_rules = OptimizationRulesConfig()
    caller_explicit_rules: Dict[str, Any] = {}
    if isinstance(rules_config, OptimizationRulesConfig):
        for f_info in dataclasses.fields(OptimizationRulesConfig):
            val = getattr(rules_config, f_info.name)
            if val != getattr(default_rules, f_info.name):
                caller_explicit_rules[f_info.name] = val

    cfg = TokenomicsConfig(
        prompts_config_path=prompts_config_path,
        optimization_rules=dataclasses.replace(rules_config) if isinstance(rules_config, OptimizationRulesConfig) else OptimizationRulesConfig(),
        optimization_prompts=optimization_prompts or {},
    )

    # Re-apply explicit caller/CLI overrides AFTER prompts_config_path is loaded so CLI flags always win
    for k, v in caller_explicit_rules.items():
        if hasattr(cfg.optimization_rules, k):
            setattr(cfg.optimization_rules, k, v)
    for k, v in (cli_rule_overrides or {}).items():
        if v is not None and hasattr(cfg.optimization_rules, k):
            setattr(cfg.optimization_rules, k, v)
    return cfg


def profile_from_trace_file(
    file_path: str,
    prompts_config_path: Optional[str] = None,
    rules_config: Optional[OptimizationRulesConfig] = None,
    optimization_prompts: Optional[Dict[str, str]] = None,
    output_format: str = "text",
    cli_rule_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """Loads a trace or JSON session file (or directory of trace files) and profiles it. Returns None on error."""
    if not os.path.exists(file_path):
        parent_dir = os.path.dirname(file_path)
        env_export_base = os.path.basename(os.environ.get("TOKENOMICS_AUTO_EXPORT_PATH", ""))
        known_live_names = {"live_adk_session.json", "session.json", "ci_eval.json"}
        if env_export_base:
            known_live_names.add(env_export_base)
        if (
            os.path.basename(file_path) in known_live_names
            and parent_dir
            and os.path.isdir(parent_dir)
        ):
            rotated = sorted(
                filter_coexisting_uninstrumented_eval_files(
                    [p for p in glob.glob(os.path.join(parent_dir, "*.json")) if not _is_non_result_adk_config_file(p)]
                ),
                key=lambda p: os.path.getmtime(p),
                reverse=True,
            )
            if rotated:
                file_path = rotated[0]
        if not os.path.exists(file_path):
            abs_path = os.path.abspath(file_path)
            hint = "" if abs_path == file_path else f" (resolved to {abs_path})"
            _emit_profile_error(f"Trace file not found: {file_path}{hint}", output_format=output_format)
            return None

    session_files_in_directory = None
    skipped_eval_files: List[str] = []
    if os.path.isdir(file_path):
        merged_candidates, skipped_eval_files = discover_directory_trace_files_with_skipped(file_path)
        candidates = sorted(
            merged_candidates,
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
        if not candidates:
            _emit_profile_error(f"No .json trace files found in directory: {file_path}", output_format=output_format)
            return None
        session_files_in_directory = len(candidates)
        skipped_note = (
            f" ({len(skipped_eval_files)} eval file(s) without token usage skipped: "
            f"{', '.join(os.path.basename(p) for p in skipped_eval_files)})"
            if skipped_eval_files
            else ""
        )
        if len(candidates) > 1 or skipped_eval_files:
            # stderr keeps `--format json` stdout machine-parseable.
            print(
                f"[INFO] {file_path} contains {len(candidates)} session trace(s){skipped_note}; "
                f"profiling the most recent: {os.path.basename(candidates[0])}. Pass -t <file> to profile a "
                "specific session, or run `eval budget` / `tokenomics assert` for an aggregate across all of them.",
                file=sys.stderr,
            )
        file_path = candidates[0]

    try:
        cfg = _build_profiler_config(
            prompts_config_path=prompts_config_path,
            rules_config=rules_config,
            optimization_prompts=optimization_prompts,
            cli_rule_overrides=cli_rule_overrides,
        )
    except (FileNotFoundError, ValueError) as cfg_err:
        _emit_profile_error(str(cfg_err), output_format=output_format)
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        turns = parse_trace_or_eval_payload(data, default_model=str(cfg.default_model))
        tokens_estimated = any(t.get("tokens_estimated") for t in turns if isinstance(t, Mapping))
        profiler = TokenProfiler(
            pricing_engine=PricingEngine(cfg),
            rules_config=cfg.optimization_rules,
            optimization_prompts=cfg.get_optimization_prompts(),
        )
        if output_format == "json":
            buf = io.StringIO()
            with redirect_stdout(buf):
                recs = profiler.profile_session(turns, session_title=os.path.basename(file_path))
            payload = {
                "file": file_path,
                "total_turns": len(turns),
                "sub_agent_breakdown": getattr(profiler, "last_agent_rollup", {}),
                "recommendations": recs,
                "turns": turns,
            }
            if session_files_in_directory is not None:
                payload["session_files_in_directory"] = session_files_in_directory
            if skipped_eval_files:
                payload["skipped_artifacts"] = [
                    {"file": p, "reason": SKIPPED_EVAL_FILE_REASON} for p in skipped_eval_files
                ]
            if tokens_estimated:
                payload["tokens_estimated"] = True
                payload["warning"] = ESTIMATED_TOKENS_WARNING
            if getattr(profiler, "last_pricing_estimated", False):
                payload["pricing_estimated"] = True
                payload["unregistered_models"] = list(getattr(profiler, "last_unregistered_models", []))
            print(json.dumps(payload, indent=2))
            return recs
        if tokens_estimated:
            print(f"\n⚠️  ESTIMATED: {ESTIMATED_TOKENS_WARNING}")
        recs = profiler.profile_session(turns, session_title=os.path.basename(file_path))
        if getattr(profiler, "last_pricing_estimated", False):
            unreg_str = ", ".join(getattr(profiler, "last_unregistered_models", []))
            print(
                f" ⚠️  ESTIMATED PRICING: Unregistered model(s) [{unreg_str}] were priced via heuristic fallback "
                "(marked ≈$). Add exact entries to models_config.json or Rate Card.\n"
            )
        return recs
    except Exception as e:
        _emit_profile_error(f"Failed to profile trace file {file_path}: {e}", output_format=output_format)
        return None


ESTIMATED_TOKENS_WARNING = (
    "this file has no token usage metadata (for example an `agents-cli eval generate` traces_<ts>.json), "
    "so token counts and costs are character-count estimates, not billed usage. Attach ADKTokenomicsPlugin "
    "to record real usage."
)


def _is_missing_bq_table_error(exc: Exception) -> bool:
    exc_name = type(exc).__name__
    msg_low = str(exc).lower()
    if exc_name == "NotFound":
        return True
    return any(tok in msg_low for tok in ("not found: table", "not found: dataset", "not found", "404"))


def _extract_text_from_adk_event_content(content_val: Any) -> str:
    unwrapped = _unwrap_json_value(content_val)
    if isinstance(unwrapped, str):
        return unwrapped.strip()
    if isinstance(unwrapped, list):
        # LLM_REQUEST message list: pick last user message text
        for msg in reversed(unwrapped):
            if isinstance(msg, Mapping) and str(msg.get("role") or "user").lower() == "user":
                txt = _extract_text_from_adk_event_content(msg.get("parts") or msg.get("content") or msg.get("text"))
                if txt:
                    return txt
        return ""
    if not isinstance(unwrapped, Mapping):
        return ""

    for direct_key in ("user_query", "text", "query", "message"):
        val = unwrapped.get(direct_key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    raw_prompt = unwrapped.get("prompt")
    if isinstance(raw_prompt, str) and raw_prompt.strip():
        return raw_prompt.strip()
    if isinstance(raw_prompt, (list, Mapping)):
        nested_p = _extract_text_from_adk_event_content(raw_prompt)
        if nested_p:
            return nested_p

    raw_contents = unwrapped.get("contents")
    if isinstance(raw_contents, list):
        nested_c = _extract_text_from_adk_event_content(raw_contents)
        if nested_c:
            return nested_c

    raw_parts = unwrapped.get("parts")
    if isinstance(raw_parts, list):
        texts = [
            str(p.get("text", "")).strip()
            for p in raw_parts
            if isinstance(p, Mapping) and p.get("text")
        ]
        if texts:
            return " ".join(t for t in texts if t).strip()
    return ""


def _infer_bq_orchestration_flow(
    sub_agent: str,
    inv_id: str,
    attr_dict: Mapping[str, Any],
    flow_hints: Mapping[Tuple[str, str], str],
    agent_tool_targets_by_inv: Mapping[str, set[str]],
    global_agent_tool_targets: set[str],
    root_agent_by_inv: Mapping[str, str],
    session_root_agent: Optional[str],
) -> str:
    is_agent_tool = bool(
        attr_dict.get("is_agent_tool")
        or sub_agent in agent_tool_targets_by_inv.get(inv_id, set())
        or sub_agent in global_agent_tool_targets
    )
    hint_parts = [
        str(attr_dict.get("orchestration_flow") or ""),
        str(attr_dict.get("workflow_type") or ""),
        str(attr_dict.get("parent_agent_type") or ""),
        str(attr_dict.get("parent_agent") or ""),
        str(attr_dict.get("agent_type") or ""),
        str(attr_dict.get("branch") or ""),
        str(flow_hints.get((inv_id, sub_agent)) or ""),
        str(sub_agent or ""),
    ]
    combined_low = " ".join(hint_parts).lower()
    is_parallel = "parallel" in combined_low
    is_loop = "loop" in combined_low
    is_seq = "sequential" in combined_low

    flow_parts: List[str] = []
    if is_agent_tool:
        flow_parts.append("AgentTool")
    if is_seq:
        flow_parts.append("Sequential")
    if is_parallel:
        flow_parts.append("Parallel")
    if is_loop:
        flow_parts.append("Loop")
    if flow_parts:
        return " • ".join(flow_parts)
    inv_root = root_agent_by_inv.get(inv_id) or session_root_agent
    if inv_root and sub_agent and sub_agent != inv_root:
        return "Sub-Agent"
    return "Root"


def _bq_row_to_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "items"):
        return dict(row.items())
    known_cols = (
        "timestamp",
        "event_type",
        "agent",
        "session_id",
        "invocation_id",
        "content",
        "attributes",
        "total_ms",
        "latency_ms",
        "sub_agent",
        "orchestration_flow",
        "user_query",
        "agent_response",
        "prompt_tokens",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "thinking_tokens",
        "usage_prompt_tokens",
        "usage_tool_use_tokens",
        "usage_cached_tokens",
        "usage_completion_tokens",
        "usage_thinking_tokens",
        "model_name",
        "model_version",
        "tools_called",
        "skills_active",
        "total_cost",
    )
    return {k: getattr(row, k) for k in known_cols if hasattr(row, k)}


def _correlate_bq_session_events(
    rows: List[Any],
    default_model: str,
    pricing_engine: PricingEngine,
) -> List[Dict[str, Any]]:
    user_query_by_inv: Dict[str, str] = {}
    user_query_by_inv_agent: Dict[Tuple[str, str], str] = {}
    latest_user_query = ""
    root_agent_by_inv: Dict[str, str] = {}
    session_root_agent: Optional[str] = None
    flow_hints: Dict[Tuple[str, str], str] = {}
    agent_tool_targets_by_inv: Dict[str, set[str]] = {}
    global_agent_tool_targets: set[str] = set()
    pending_tools: Dict[Tuple[str, str], List[str]] = {}
    pending_skills: Dict[Tuple[str, str], List[str]] = {}
    last_turn_by_inv_agent: Dict[Tuple[str, str], Dict[str, Any]] = {}
    last_turn_by_inv: Dict[str, Dict[str, Any]] = {}
    turns: List[Dict[str, Any]] = []

    for raw_row in rows:
        d = _bq_row_to_dict(raw_row)
        content_dict = _unwrap_json_dict(d.get("content"))
        attr_dict = _unwrap_json_dict(d.get("attributes"))
        if content_dict:
            d["content"] = content_dict
        if attr_dict:
            d["attributes"] = attr_dict

        raw_ev_type = d.get("event_type")
        ev_type = (
            str(raw_ev_type).strip().upper()
            if (raw_ev_type is not None and str(raw_ev_type).strip())
            else "LLM_RESPONSE"
        )
        inv_id = str(d.get("invocation_id") or attr_dict.get("invocation_id") or "").strip()
        ev_agent = str(attr_dict.get("sub_agent") or d.get("sub_agent") or d.get("agent") or "").strip()

        if ev_agent and ev_agent.lower() != "user":
            if session_root_agent is None:
                session_root_agent = ev_agent
            if inv_id and inv_id not in root_agent_by_inv:
                root_agent_by_inv[inv_id] = ev_agent

        hint_val = " ".join(
            str(attr_dict.get(k) or "")
            for k in ("orchestration_flow", "workflow_type", "parent_agent_type", "parent_agent", "agent_type", "branch")
            if attr_dict.get(k)
        ).strip()
        if hint_val and ev_agent:
            flow_hints[(inv_id, ev_agent)] = hint_val

        if ev_type == "USER_MESSAGE_RECEIVED":
            u_txt = _extract_text_from_adk_event_content(content_dict or d.get("content"))
            if u_txt:
                latest_user_query = u_txt
                if inv_id:
                    user_query_by_inv[inv_id] = u_txt
            continue

        if ev_type == "LLM_REQUEST":
            req_txt = _extract_text_from_adk_event_content(content_dict or d.get("content"))
            if req_txt:
                if not latest_user_query:
                    latest_user_query = req_txt
                if inv_id and inv_id not in user_query_by_inv:
                    user_query_by_inv[inv_id] = req_txt
                if ev_agent:
                    user_query_by_inv_agent[(inv_id, ev_agent)] = req_txt
            continue

        if ev_type in ("TOOL_STARTING", "TOOL_COMPLETED", "TOOL_ERROR"):
            t_name = str(
                content_dict.get("tool")
                or content_dict.get("tool_name")
                or content_dict.get("name")
                or attr_dict.get("tool_name")
                or attr_dict.get("tool")
                or ""
            ).strip()
            t_args = (
                content_dict.get("args")
                if content_dict.get("args") is not None
                else (
                    content_dict.get("arguments")
                    if content_dict.get("arguments") is not None
                    else (
                        content_dict.get("tool_args")
                        if content_dict.get("tool_args") is not None
                        else attr_dict.get("tool_args", attr_dict.get("args"))
                    )
                )
            )
            if t_name:
                global_agent_tool_targets.add(t_name)
                if inv_id:
                    agent_tool_targets_by_inv.setdefault(inv_id, set()).add(t_name)
                sk_name = _extract_skill_from_tool(t_name, t_args)
                target_turn = last_turn_by_inv_agent.get((inv_id, ev_agent)) or (
                    last_turn_by_inv.get(inv_id) if inv_id else None
                )
                if target_turn is not None:
                    tc_list = target_turn.setdefault("tools_called", [])
                    if t_name not in tc_list:
                        tc_list.append(t_name)
                    if sk_name:
                        sk_list = target_turn.setdefault("skills_active", [])
                        if sk_name not in sk_list:
                            sk_list.append(sk_name)
                else:
                    pt_list = pending_tools.setdefault((inv_id, ev_agent), [])
                    if t_name not in pt_list:
                        pt_list.append(t_name)
                    if sk_name:
                        ps_list = pending_skills.setdefault((inv_id, ev_agent), [])
                        if sk_name not in ps_list:
                            ps_list.append(sk_name)
            continue

        if ev_type != "LLM_RESPONSE":
            continue

        parsed = parse_bq_turn_record(d, default_model=default_model, pricing_engine=pricing_engine)
        if not parsed:
            continue

        sub_ag = str(parsed.get("sub_agent") or ev_agent or "agent")
        turn_inv = str(parsed.get("invocation_id") or inv_id or "")

        # Extract embedded function calls in LLM_RESPONSE content (upstream ADK response.parts / parts)
        resp_obj = _unwrap_json_dict(content_dict.get("response"))
        fc_tools_1, fc_skills_1 = _extract_function_calls_from_parts(content_dict.get("parts"))
        fc_tools_2, fc_skills_2 = _extract_function_calls_from_parts(resp_obj.get("parts"))
        for fc_t in fc_tools_1 + fc_tools_2:
            global_agent_tool_targets.add(fc_t)
            if turn_inv:
                agent_tool_targets_by_inv.setdefault(turn_inv, set()).add(fc_t)
            if fc_t not in parsed["tools_called"]:
                parsed["tools_called"].append(fc_t)
        for fc_s in fc_skills_1 + fc_skills_2:
            if fc_s not in parsed["skills_active"]:
                parsed["skills_active"].append(fc_s)

        # Flush any tools/skills recorded prior to this LLM_RESPONSE for (turn_inv, sub_ag)
        for p_t in pending_tools.pop((turn_inv, sub_ag), []):
            if p_t not in parsed["tools_called"]:
                parsed["tools_called"].append(p_t)
        for p_s in pending_skills.pop((turn_inv, sub_ag), []):
            if p_s not in parsed["skills_active"]:
                parsed["skills_active"].append(p_s)

        # Correlate user_query when missing or generic placeholder
        cur_uq = str(parsed.get("user_query") or "").strip()
        if not cur_uq or cur_uq == "Turn":
            correlated_uq = (
                user_query_by_inv.get(turn_inv)
                or user_query_by_inv_agent.get((turn_inv, sub_ag))
                or latest_user_query
                or "Turn"
            )
            parsed["user_query"] = correlated_uq

        # Infer orchestration_flow when missing
        if not str(parsed.get("orchestration_flow") or "").strip():
            parsed["orchestration_flow"] = _infer_bq_orchestration_flow(
                sub_agent=sub_ag,
                inv_id=turn_inv,
                attr_dict=attr_dict,
                flow_hints=flow_hints,
                agent_tool_targets_by_inv=agent_tool_targets_by_inv,
                global_agent_tool_targets=global_agent_tool_targets,
                root_agent_by_inv=root_agent_by_inv,
                session_root_agent=session_root_agent,
            )

        turns.append(parsed)
        last_turn_by_inv_agent[(turn_inv, sub_ag)] = parsed
        if turn_inv:
            last_turn_by_inv[turn_inv] = parsed

    return turns


def profile_from_bigquery(
    session_id: str,
    project_id: Optional[str],
    dataset_id: str = "bq_adk_ds",
    table_id: str = "adk_agent_events",
    prompts_config_path: Optional[str] = None,
    rules_config: Optional[OptimizationRulesConfig] = None,
    optimization_prompts: Optional[Dict[str, str]] = None,
    output_format: str = "text",
    cli_rule_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """Queries BigQuery for an exact session ID, correlates ADK lifecycle events, and profiles the turns. Returns None on error."""
    if not session_id or not str(session_id).strip():
        _emit_profile_error("Session ID must be non-empty.", output_format=output_format)
        return None

    if not project_id:
        _emit_profile_error(
            "No GCP project specified for BigQuery profiling. Pass --project <PROJECT_ID> or set GOOGLE_CLOUD_PROJECT.",
            output_format=output_format,
        )
        return None

    try:
        cfg = _build_profiler_config(
            prompts_config_path=prompts_config_path,
            rules_config=rules_config,
            optimization_prompts=optimization_prompts,
            cli_rule_overrides=cli_rule_overrides,
        )
        project_id = validate_bq_project_id(project_id)
        dataset_id = validate_bq_identifier(dataset_id, "dataset_id")
        table_id = validate_bq_identifier(table_id, "table_id")
    except (FileNotFoundError, ValueError) as ve:
        _emit_profile_error(str(ve), output_format=output_format)
        return None

    try:
        from google.cloud import bigquery
    except ImportError as ie:
        _emit_profile_error(
            f"google-cloud-bigquery is not installed ({ie}). {bigquery_install_hint()}",
            output_format=output_format,
        )
        return None

    try:
        client = bigquery.Client(project=project_id)
        pricing_engine = PricingEngine(cfg)

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
            ]
        )
        turns: List[Dict[str, Any]] = []
        active_tbl = table_id
        first_existing_tbl: Optional[str] = None
        is_explicit_non_default_table = table_id != "adk_agent_events"

        for idx_cand, candidate_tbl in enumerate(dict.fromkeys([table_id, "adk_agent_events", "agent_events"])):
            if not candidate_tbl:
                continue
            validated_tbl = validate_bq_identifier(candidate_tbl, "table_id")
            query = f"""
                SELECT
                    timestamp,
                    event_type,
                    agent,
                    session_id,
                    invocation_id,
                    content,
                    attributes,
                    COALESCE(
                        SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64),
                        SAFE_CAST(JSON_VALUE(latency_ms, '$') AS INT64),
                        SAFE_CAST(ROUND(SAFE_CAST(TRIM(TO_JSON_STRING(latency_ms), '"') AS FLOAT64)) AS INT64),
                        0
                    ) AS total_ms,
                    latency_ms
                FROM `{project_id}.{dataset_id}.{validated_tbl}`
                WHERE session_id = @session_id
                ORDER BY timestamp ASC
            """
            try:
                rows = list(client.query(query, job_config=job_config).result())
                if first_existing_tbl is None:
                    first_existing_tbl = validated_tbl
                candidate_turns = _correlate_bq_session_events(
                    rows,
                    default_model=str(cfg.default_model),
                    pricing_engine=pricing_engine,
                )
                if candidate_turns:
                    turns = candidate_turns
                    active_tbl = validated_tbl
                    break
                # Stop table fallback immediately if an explicit non-default --table query succeeded with 0 rows
                if idx_cand == 0 and is_explicit_non_default_table:
                    active_tbl = validated_tbl
                    break
            except Exception as tbl_exc:
                if _is_missing_bq_table_error(tbl_exc):
                    continue
                raise

        if not turns:
            if first_existing_tbl is not None:
                active_tbl = first_existing_tbl
            _emit_profile_error(
                f"No BigQuery records found for session '{session_id}' in {project_id}.{dataset_id}.{active_tbl}",
                output_format=output_format,
            )
            return None

        profiler = TokenProfiler(
            pricing_engine=pricing_engine,
            rules_config=cfg.optimization_rules,
            optimization_prompts=cfg.get_optimization_prompts(),
        )
        if output_format == "json":
            buf = io.StringIO()
            with redirect_stdout(buf):
                recs = profiler.profile_session(turns, session_title=f"BigQuery Session: {session_id}")
            bq_payload: Dict[str, Any] = {
                "session_id": session_id,
                "source": f"{project_id}.{dataset_id}.{active_tbl}",
                "total_turns": len(turns),
                "sub_agent_breakdown": getattr(profiler, "last_agent_rollup", {}),
                "recommendations": recs,
                "turns": turns,
            }
            if getattr(profiler, "last_pricing_estimated", False):
                bq_payload["pricing_estimated"] = True
                bq_payload["unregistered_models"] = list(getattr(profiler, "last_unregistered_models", []))
            print(json.dumps(bq_payload, indent=2, default=str))
            return recs
        recs = profiler.profile_session(turns, session_title=f"BigQuery Session: {session_id}")
        if getattr(profiler, "last_pricing_estimated", False):
            unreg_str = ", ".join(getattr(profiler, "last_unregistered_models", []))
            print(
                f" ⚠️  ESTIMATED PRICING: Unregistered model(s) [{unreg_str}] were priced via heuristic fallback "
                "(marked ≈$). Add exact entries to models_config.json or Rate Card.\n"
            )
        return recs
    except Exception as e:
        _emit_profile_error(f"BigQuery profiling query failed: {e}", output_format=output_format)
        return None
