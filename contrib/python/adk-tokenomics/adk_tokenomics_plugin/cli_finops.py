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

# ruff: noqa: S608
"""
ADK Tokenomics - CLI Executive FinOps Summary
Queries BigQuery for fleet-wide token spend, optimization savings, and top cost centers.
"""

import json
from typing import Any

from adk_tokenomics_plugin.core.config import (
    validate_bq_identifier,
    validate_bq_project_id,
)


def _emit_finops_error(msg: str, output_format: str = "text") -> None:
    if output_format == "json":
        print(json.dumps({"error": msg}))
    else:
        print(f"[ERROR] {msg}")


def _should_fallback_to_base_table(exc: Exception) -> bool:
    """Returns True when a BigQuery view query failed due to missing view or missing schema columns."""
    exc_name = type(exc).__name__
    msg_low = str(exc).lower()
    if exc_name == "NotFound":
        return True
    return any(
        token in msg_low
        for token in (
            "not found",
            "404",
            "unrecognized name",
        )
    )


def _is_missing_bq_table_error(exc: Exception) -> bool:
    exc_name = type(exc).__name__
    msg_low = str(exc).lower()
    if exc_name == "NotFound":
        return True
    return any(
        tok in msg_low
        for tok in (
            "not found: table",
            "not found: dataset",
            "not found",
            "404",
        )
    )


def _is_missing_tool_use_col_error(exc: Exception) -> bool:
    msg_low = str(exc).lower()
    return "unrecognized name" in msg_low and "usage_tool_use_tokens" in msg_low


_BASE_PROMPT_EXPR = (
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
)

_BASE_CACHED_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64), "
    "0)"
)

_BASE_OUT_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64), "
    "0)"
)

_BASE_THINK_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64), "
    "SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64), "
    "0)"
)

_BASE_LATENCY_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), "
    "SAFE_CAST(JSON_VALUE(latency_ms, '$') AS INT64), "
    "SAFE_CAST(ROUND(SAFE_CAST(TRIM(TO_JSON_STRING(latency_ms), '\"') AS FLOAT64)) AS INT64), "
    "0)"
)

_BASE_MODEL_EXPR = (
    "COALESCE("
    "JSON_VALUE(attributes, '$.model_version'), "
    "JSON_VALUE(attributes, '$.model'), "
    "JSON_VALUE(content, '$.model_version'), "
    "'gemini-3.8-flash')"
)


def _build_view_totals_sql(
    full_view: str, days: int, include_tool_use_col: bool = True
) -> str:
    prompt_col_expr = (
        "COALESCE(usage_prompt_tokens, 0) + COALESCE(usage_tool_use_tokens, 0)"
        if include_tool_use_col
        else "COALESCE(usage_prompt_tokens, 0)"
    )
    return f"""
        WITH filtered AS (
            SELECT *
            FROM `{full_view}`
            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
        )
        SELECT 
            COALESCE(model_version, 'gemini-3.8-flash') AS model_name,
            COUNT(*) AS total_turns,
            COUNT(DISTINCT session_id) AS total_sessions,
            (SELECT COUNT(DISTINCT session_id) FROM filtered) AS fleet_unique_sessions,
            COALESCE(SUM({prompt_col_expr}), 0) AS total_prompt_tokens,
            COALESCE(SUM(COALESCE(usage_cached_tokens, 0)), 0) AS total_cached_tokens,
            COALESCE(SUM(COALESCE(usage_completion_tokens, 0)), 0) AS total_output_tokens,
            COALESCE(SUM(COALESCE(usage_thinking_tokens, 0)), 0) AS total_thinking_tokens,
            COALESCE(SUM(COALESCE(total_ms, 0)), 0) AS total_latency_ms,
            COUNTIF(COALESCE(total_ms, 0) > 0) AS latency_turns
        FROM filtered
        GROUP BY model_name
    """


def _build_base_totals_sql(full_table: str, days: int) -> str:
    return f"""
        WITH filtered AS (
            SELECT *
            FROM `{full_table}`
            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
              AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
        )
        SELECT 
            {_BASE_MODEL_EXPR} AS model_name,
            COUNT(*) AS total_turns,
            COUNT(DISTINCT session_id) AS total_sessions,
            (SELECT COUNT(DISTINCT session_id) FROM filtered) AS fleet_unique_sessions,
            COALESCE(SUM({_BASE_PROMPT_EXPR}), 0) AS total_prompt_tokens,
            COALESCE(SUM({_BASE_CACHED_EXPR}), 0) AS total_cached_tokens,
            COALESCE(SUM({_BASE_OUT_EXPR}), 0) AS total_output_tokens,
            COALESCE(SUM({_BASE_THINK_EXPR}), 0) AS total_thinking_tokens,
            COALESCE(SUM({_BASE_LATENCY_EXPR}), 0) AS total_latency_ms,
            COUNTIF({_BASE_LATENCY_EXPR} > 0) AS latency_turns
        FROM filtered
        GROUP BY model_name
    """


def _row_get(row: Any, key: str, default: Any = 0) -> Any:
    if isinstance(row, dict):
        val = row.get(key, default)
    else:
        val = getattr(row, key, default)
    return default if val is None else val


def run_finops_summary_cli(
    project_id: str | None,
    dataset_id: str = "bq_adk_ds",
    view_id: str = "v_llm_response",
    table_id: str = "adk_agent_events",
    days: int = 7,
    limit_agents: int = 5,
    output_format: str = "text",
) -> int:
    """Executes a fast FinOps summary query against BigQuery and displays a terminal executive report."""
    if not project_id:
        _emit_finops_error(
            "No GCP project specified. Pass --project <PROJECT_ID> or set GOOGLE_CLOUD_PROJECT.",
            output_format=output_format,
        )
        return 1

    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        _emit_finops_error(
            f"--days must be a positive integer (>= 1), got {days!r}.",
            output_format=output_format,
        )
        return 1

    if (
        isinstance(limit_agents, bool)
        or not isinstance(limit_agents, int)
        or limit_agents < 1
    ):
        _emit_finops_error(
            f"--limit must be a positive integer (>= 1), got {limit_agents!r}.",
            output_format=output_format,
        )
        return 1

    try:
        project_id = validate_bq_project_id(project_id)
        dataset_id = validate_bq_identifier(dataset_id, "dataset_id")
        view_id = validate_bq_identifier(view_id, "view_id")
        table_id = validate_bq_identifier(table_id, "table_id")
    except ValueError as ve:
        _emit_finops_error(str(ve), output_format=output_format)
        return 1

    try:
        from google.cloud import bigquery
    except ImportError as ie:
        _emit_finops_error(
            f"google-cloud-bigquery is not installed ({ie}). Install via: pip install 'adk-tokenomics-plugin[bigquery]'",
            output_format=output_format,
        )
        return 1

    try:
        client = bigquery.Client(project=project_id)

        if output_format != "json":
            print("\n" + "=" * 80)
            print(
                f" 💼 ADK FLEET FINOPS OBSERVATORY | {project_id}.{dataset_id}"
            )
            print(f"    Reporting Window: Past {days} Days")
            print("=" * 80)

        used_base_table_fallback = False
        view_has_tool_use_col = True
        full_view = f"{project_id}.{dataset_id}.{view_id}"
        res: list[Any] | None = None

        try:
            res = list(
                client.query(
                    _build_view_totals_sql(
                        full_view, days, include_tool_use_col=True
                    )
                ).result()
            )
        except Exception as view_exc:
            if _is_missing_tool_use_col_error(view_exc):
                view_has_tool_use_col = False
                try:
                    res = list(
                        client.query(
                            _build_view_totals_sql(
                                full_view, days, include_tool_use_col=False
                            )
                        ).result()
                    )
                except Exception as legacy_view_exc:
                    if not _should_fallback_to_base_table(legacy_view_exc):
                        raise
                    used_base_table_fallback = True
            elif _should_fallback_to_base_table(view_exc):
                used_base_table_fallback = True
            else:
                raise

        if used_base_table_fallback:
            is_explicit_non_default_table = table_id != "adk_agent_events"
            base_candidates = [
                c
                for c in dict.fromkeys(
                    [table_id, "adk_agent_events", "agent_events"]
                )
                if c
            ]
            first_existing_source: str | None = None
            last_missing_exc: Exception | None = None

            for idx_cand, candidate_tbl in enumerate(base_candidates):
                validated_tbl = validate_bq_identifier(
                    candidate_tbl, "table_id"
                )
                candidate_full = f"{project_id}.{dataset_id}.{validated_tbl}"
                try:
                    cand_res = list(
                        client.query(
                            _build_base_totals_sql(candidate_full, days)
                        ).result()
                    )
                    if first_existing_source is None:
                        first_existing_source = candidate_full
                    cand_turns = (
                        sum(
                            int(_row_get(r, "total_turns", 0)) for r in cand_res
                        )
                        if cand_res
                        else 0
                    )
                    if cand_turns > 0 or (
                        idx_cand == 0 and is_explicit_non_default_table
                    ):
                        res = cand_res
                        full_view = candidate_full
                        break
                    if res is None:
                        res = cand_res
                        full_view = candidate_full
                except Exception as base_exc:
                    if _is_missing_bq_table_error(base_exc):
                        last_missing_exc = base_exc
                        continue
                    raise

            if res is None:
                if last_missing_exc is not None:
                    raise last_missing_exc
                res = []
            elif first_existing_source is not None and (
                sum(int(_row_get(r, "total_turns", 0)) for r in res) == 0
            ):
                full_view = first_existing_source

        total_turns_cnt = (
            sum(int(_row_get(r, "total_turns", 0)) for r in res) if res else 0
        )
        if not res or total_turns_cnt == 0:
            if output_format == "json":
                print(
                    json.dumps(
                        {
                            "source": full_view,
                            "days": days,
                            "total_turns": 0,
                            "total_sessions": 0,
                            "total_spend_usd": 0.0,
                            "top_agents": [],
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    f"\n[INFO] No LLM activity recorded in `{full_view}` in the past {days} days.\n"
                )
            return 0

        from adk_tokenomics_plugin.core.pricing import PricingEngine

        pricing = PricingEngine()
        default_model = pricing.config.default_model

        prompt_tokens = 0
        cached_tokens = 0
        fresh_tokens = 0
        output_tokens = 0
        thinking_tokens = 0
        total_latency_ms = 0
        total_latency_turns = 0
        summed_model_sessions = 0
        fleet_unique_sessions: int | None = None
        actual_spend = 0.0
        savings_dollars = 0.0

        for r in res:
            m_id = str(
                _row_get(r, "model_name", default_model) or default_model
            )
            p_tok = int(_row_get(r, "total_prompt_tokens", 0))
            c_tok = int(_row_get(r, "total_cached_tokens", 0))
            f_tok = max(0, p_tok - c_tok)
            o_tok = int(_row_get(r, "total_output_tokens", 0))
            t_tok = int(_row_get(r, "total_thinking_tokens", 0))
            row_turns = int(_row_get(r, "total_turns", 0))
            row_lat_ms = int(_row_get(r, "total_latency_ms", 0))
            prompt_tokens += p_tok
            cached_tokens += c_tok
            fresh_tokens += f_tok
            output_tokens += o_tok
            thinking_tokens += t_tok
            total_latency_ms += row_lat_ms
            raw_lat_turns = (
                r.get("latency_turns")
                if isinstance(r, dict)
                else getattr(r, "latency_turns", None)
            )
            if isinstance(raw_lat_turns, (int, float)) and not isinstance(
                raw_lat_turns, bool
            ):
                total_latency_turns += int(raw_lat_turns)
            elif row_lat_ms > 0:
                total_latency_turns += row_turns
            summed_model_sessions += int(_row_get(r, "total_sessions", 0))
            raw_fleet_sess = (
                getattr(r, "fleet_unique_sessions", None)
                if not isinstance(r, dict)
                else r.get("fleet_unique_sessions")
            )
            if isinstance(raw_fleet_sess, (int, float)) and not isinstance(
                raw_fleet_sess, bool
            ):
                fleet_unique_sessions = int(raw_fleet_sess)
            cost_info = pricing.calculate_turn_cost(
                model_id=m_id,
                input_tokens=f_tok,
                cached_tokens=c_tok,
                output_tokens=o_tok,
                thinking_tokens=t_tok,
            )
            actual_spend += cost_info["total_cost"]
            savings_dollars += cost_info["savings_dollars"]

        total_sessions = (
            fleet_unique_sessions
            if fleet_unique_sessions is not None
            else summed_model_sessions
        )
        total_tokens = prompt_tokens + output_tokens + thinking_tokens
        cache_rate = (
            (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0.0
        )
        uncached_total = actual_spend + savings_dollars
        savings_pct = (
            (savings_dollars / uncached_total * 100.0)
            if uncached_total > 0
            else 0.0
        )

        unit_cost_1k = (
            (actual_spend / total_turns_cnt * 1000)
            if total_turns_cnt > 0
            else 0.0
        )
        avg_latency = (
            (total_latency_ms / total_latency_turns)
            if total_latency_turns > 0
            else 0.0
        )

        if output_format != "json":
            print("\n📊 EXECUTIVE KPI SUMMARY:")
            print(f" • Total Fleet Spend (Est.) : ${actual_spend:,.4f} USD")
            print(
                f" • Optimization Savings     : ${savings_dollars:,.4f} USD ({savings_pct:.1f}% saved vs. baseline)"
            )
            print(
                f" • Total Turns Processed    : {total_turns_cnt:,} across {total_sessions:,} sessions"
            )
            print(f" • Total Tokens Consumed    : {total_tokens:,}")
            print(f"   ├─ Fresh Input Tokens    : {fresh_tokens:,}")
            print(
                f"   ├─ Cached Tokens         : {cached_tokens:,} ({cache_rate:.1f}% Cache Hit Rate)"
            )
            print(f"   ├─ Output Tokens         : {output_tokens:,}")
            print(f"   └─ Thinking Tokens       : {thinking_tokens:,}")
            print(f" • Unit Cost per 1,000 Turns : ${unit_cost_1k:.4f}")
            print(f" • Avg Turn Latency         : {avg_latency:,.0f} ms\n")
            print("─" * 80)
            print(f"🏆 TOP {limit_agents} COSTLIEST AGENT APPLICATIONS:")
            print("─" * 80)

        if not used_base_table_fallback:
            view_ag_prompt_expr = (
                "COALESCE(usage_prompt_tokens, 0) + COALESCE(usage_tool_use_tokens, 0)"
                if view_has_tool_use_col
                else "COALESCE(usage_prompt_tokens, 0)"
            )
            query_agents = f"""
                SELECT 
                    COALESCE(agent, 'default_agent') AS agent_name,
                    COALESCE(model_version, 'gemini-3.8-flash') AS model_name,
                    COUNT(*) AS turns,
                    COALESCE(SUM({view_ag_prompt_expr}), 0) AS prompt_tok,
                    COALESCE(SUM(COALESCE(usage_cached_tokens, 0)), 0) AS cached_tok,
                    COALESCE(SUM(COALESCE(usage_completion_tokens, 0)), 0) AS out_tok,
                    COALESCE(SUM(COALESCE(usage_thinking_tokens, 0)), 0) AS thk_tok
                FROM `{full_view}`
                WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
                GROUP BY agent_name, model_name
            """
        else:
            query_agents = f"""
                SELECT 
                    COALESCE(agent, 'default_agent') AS agent_name,
                    {_BASE_MODEL_EXPR} AS model_name,
                    COUNT(*) AS turns,
                    COALESCE(SUM({_BASE_PROMPT_EXPR}), 0) AS prompt_tok,
                    COALESCE(SUM({_BASE_CACHED_EXPR}), 0) AS cached_tok,
                    COALESCE(SUM({_BASE_OUT_EXPR}), 0) AS out_tok,
                    COALESCE(SUM({_BASE_THINK_EXPR}), 0) AS thk_tok
                FROM `{full_view}`
                WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
                  AND (event_type = 'LLM_RESPONSE' OR event_type IS NULL)
                GROUP BY agent_name, model_name
            """

        top_agents_payload: list[dict[str, Any]] = []
        try:
            agent_rows = list(client.query(query_agents).result())
            if output_format != "json":
                print(
                    f"{'Agent Name':<32} | {'Turns':<8} | {'Tokens':<14} | {'Est. Cost ($)':<14} | {'Share (%)'}"
                )
                print("-" * 80)
            agg_by_agent: dict[str, dict[str, Any]] = {}
            for ar in agent_rows:
                ag_name = str(_row_get(ar, "agent_name", "default_agent"))
                ag_turns = int(_row_get(ar, "turns", 0))
                p = int(_row_get(ar, "prompt_tok", 0))
                c = int(_row_get(ar, "cached_tok", 0))
                f = max(0, p - c)
                o = int(_row_get(ar, "out_tok", 0))
                th = int(_row_get(ar, "thk_tok", 0))
                ag_model = str(
                    _row_get(ar, "model_name", default_model) or default_model
                )
                a_cost = pricing.calculate_cost(ag_model, f, c, o, th)
                bucket = agg_by_agent.setdefault(
                    ag_name,
                    {
                        "agent_name": ag_name,
                        "turns": 0,
                        "tokens": 0,
                        "cost": 0.0,
                    },
                )
                bucket["turns"] += ag_turns
                bucket["tokens"] += p + o + th
                bucket["cost"] += a_cost

            computed_agents: list[dict[str, Any]] = []
            for item in agg_by_agent.values():
                item["share"] = (
                    (item["cost"] / actual_spend * 100)
                    if actual_spend > 0
                    else 0.0
                )
                computed_agents.append(item)
            computed_agents.sort(
                key=lambda item: (item["cost"], item["tokens"]), reverse=True
            )
            for item in computed_agents[:limit_agents]:
                ag_name = item["agent_name"]
                ag_turns = item["turns"]
                tot_tok = item["tokens"]
                a_cost = item["cost"]
                share = item["share"]
                top_agents_payload.append(
                    {
                        "agent_name": ag_name,
                        "turns": ag_turns,
                        "tokens": tot_tok,
                        "estimated_cost_usd": round(a_cost, 6),
                        "share_pct": round(share, 2),
                    }
                )
                if output_format != "json":
                    print(
                        f"{ag_name:<32} | {ag_turns:<8,d} | {tot_tok:<14,d} | ${a_cost:<13.4f} | {share:5.1f}%"
                    )
        except Exception as e:
            if output_format != "json":
                print(f"[WARN] Could not retrieve per-agent breakdown: {e}")

        if output_format == "json":
            print(
                json.dumps(
                    {
                        "source": full_view,
                        "days": days,
                        "total_turns": total_turns_cnt,
                        "total_sessions": total_sessions,
                        "total_spend_usd": round(actual_spend, 6),
                        "optimization_savings_usd": round(savings_dollars, 6),
                        "cache_hit_rate_pct": round(cache_rate, 2),
                        "total_tokens": total_tokens,
                        "avg_latency_ms": round(avg_latency, 1),
                        "top_agents": top_agents_payload,
                    },
                    indent=2,
                )
            )
        else:
            print("=" * 80)
            print("\n📈 View Full Dashboards:")
            print(
                " • BigQuery Data Agent : Ask natural language questions in Google Cloud Console"
            )
            print(
                " • Standalone Server   : Run `adk-tokenomics web` to launch local web observatory\n"
            )
        return 0

    except Exception as e:
        _emit_finops_error(
            f"BigQuery FinOps summary failed: {e}", output_format=output_format
        )
        return 1
