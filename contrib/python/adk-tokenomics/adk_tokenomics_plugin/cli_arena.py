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
ADK Tokenomics - CLI Model Arena (Price vs. Quality Pareto Evaluator)
Compares candidate models on cost, latency, output tokens, and eval accuracy to find the optimal Pareto frontier.
"""

import json
import os

from adk_tokenomics_plugin.core.pricing import PricingEngine

DEFAULT_MODELS = [
    "publishers/google/models/gemini-3.8-flash",
    "publishers/google/models/gemini-4-argon",
    "claude-3-5-haiku",
    "claude-sonnet-5",
    "meta/llama-3.3-70b-instruct",
    "deepseek/deepseek-r1",
]


class ModelArena:
    def __init__(self, pricing_engine: PricingEngine | None = None):
        self.pricing = pricing_engine or PricingEngine()

    def compare_models(
        self,
        models: list[str] | None = None,
        eval_results_file: str | None = None,
        dataset_name: str | None = None,
        avg_prompt_tokens: int = 2000,
        avg_cached_tokens: int = 1500,
        avg_output_tokens: int = 400,
    ):
        """Renders the Price vs. Quality / Efficiency comparison table."""
        model_list = models or DEFAULT_MODELS
        print("\n" + "=" * 110)
        title = " ⚖️  ADK MODEL PRICE & QUALITY ARENA"
        if dataset_name:
            title += f" | Benchmark: {dataset_name}"
        print(title)
        print("=" * 110)

        header = f"{'Model Name':<38} | {'Input $/1M':<11} | {'Cache $/1M':<11} | {'Output $/1M':<12} | {'Cost/1K Turns':<14} | {'Accuracy':<9} | {'Value Score'}"
        print(header)
        print("-" * 110)

        # Ingest eval results if provided
        eval_scores = {}
        if eval_results_file and os.path.exists(eval_results_file):
            try:
                with open(eval_results_file) as f:
                    eval_data = json.load(f)
                    if isinstance(eval_data, dict):
                        # agents-cli grade results format
                        summary = eval_data.get("summary") or eval_data
                        for k, v in summary.items():
                            if isinstance(v, (int, float)):
                                eval_scores[k] = float(v)
            except Exception as e:
                print(
                    f"[WARN] Could not parse eval results file {eval_results_file}: {e}"
                )

        rows = []
        for m in model_list:
            clean_name = m.replace("publishers/google/models/", "").replace(
                "models/", ""
            )
            rates = self.pricing.get_rates(m)

            in_rate = rates["input_per_million"]
            cache_rate = rates["cached_per_million"]
            out_rate = rates["output_per_million"]

            # Compute typical cost for 1,000 turns based on profile
            fresh_tokens = max(0, avg_prompt_tokens - avg_cached_tokens)
            turn_cost = self.pricing.calculate_cost(
                m,
                input_tokens=fresh_tokens,
                cached_tokens=avg_cached_tokens,
                output_tokens=avg_output_tokens,
                thinking_tokens=0,
            )
            cost_1k = turn_cost * 1000

            # Find accuracy if evaluated
            acc_score = eval_scores.get(clean_name, eval_scores.get(m, None))
            if acc_score is not None:
                acc_str = (
                    f"{acc_score * 100:.1f}%"
                    if acc_score <= 1.0
                    else f"{acc_score:.1f}%"
                )
                acc_val = acc_score if acc_score <= 1.0 else (acc_score / 100.0)
                if cost_1k <= 0.0:
                    value_score = float("inf")
                    value_str = "∞ (Free)"
                else:
                    value_score = (acc_val * 100.0) / cost_1k
                    value_str = f"{value_score:5.1f} pts/$"
            else:
                acc_str = "—"
                acc_val = 0.0
                if cost_1k <= 0.0:
                    value_score = float("inf")
                    value_str = "∞ (Free)"
                else:
                    value_score = 100.0 / cost_1k
                    value_str = f"({value_score:5.1f} baseline)"

            rows.append(
                {
                    "model": clean_name,
                    "in_rate": in_rate,
                    "cache_rate": cache_rate,
                    "out_rate": out_rate,
                    "cost_1k": cost_1k,
                    "acc_str": acc_str,
                    "acc_val": acc_val,
                    "value_str": value_str,
                    "value_score": value_score,
                }
            )

        # Sort by value score descending (ranking $0 self-hosted models first via float('inf'))
        rows.sort(
            key=lambda x: (x["value_score"], x.get("acc_val", 0.0)),
            reverse=True,
        )

        best_model = rows[0]["model"] if rows else ""

        for idx, r in enumerate(rows):
            star = " ⭐ (Best Value)" if idx == 0 else ""
            line = f"{r['model']:<38} | ${r['in_rate']:<10.2f} | ${r['cache_rate']:<10.3f} | ${r['out_rate']:<11.2f} | ${r['cost_1k']:<13.4f} | {r['acc_str']:<9} | {r['value_str']}{star}"
            print(line)

        print("=" * 110)
        print("\n💡 ARENA RECOMMENDATION:")
        print(
            f" • Most Cost-Efficient: `{best_model}` gives the highest performance per dollar."
        )
        print(
            " • Pro-Tip: Use Hybrid Routing (`SequentialAgent`) to route simple intent turns to Gemini 3.8 Flash before escalating to Gemini 4 Argon.\n"
        )


def run_arena_cli(
    models_str: str | None = None,
    results_file: str | None = None,
    dataset: str | None = None,
    avg_input: int = 2000,
    avg_output: int = 400,
    cache_ratio: float = 0.75,
):
    import math

    models = (
        [m.strip() for m in models_str.split(",") if m.strip()]
        if models_str
        else None
    )
    safe_ratio = (
        cache_ratio
        if (
            isinstance(cache_ratio, (int, float))
            and not isinstance(cache_ratio, bool)
            and math.isfinite(float(cache_ratio))
        )
        else 0.75
    )
    avg_cached = int(max(0, avg_input) * max(0.0, min(1.0, float(safe_ratio))))
    arena = ModelArena()
    arena.compare_models(
        models=models,
        eval_results_file=results_file,
        dataset_name=dataset,
        avg_prompt_tokens=max(0, avg_input),
        avg_cached_tokens=avg_cached,
        avg_output_tokens=max(0, avg_output),
    )
