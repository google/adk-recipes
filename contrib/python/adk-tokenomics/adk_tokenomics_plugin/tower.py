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
Tokenomics SDK - TokenControlTower Main Hub
High-level plug-and-play interface for connecting Tokenomics to any AI Agent lifecycle.
"""

import dataclasses
import sys
from collections.abc import Callable
from typing import Any

from adk_tokenomics_plugin.core.config import (
    OptimizationRulesConfig,
    TokenomicsConfig,
)
from adk_tokenomics_plugin.core.sinks import BaseSink, create_sink
from adk_tokenomics_plugin.core.tracker import TokenTracker
from adk_tokenomics_plugin.finops.bq_schema import provision_bigquery_table
from adk_tokenomics_plugin.middleware.adk_callback import ADKTokenomicsPlugin
from adk_tokenomics_plugin.prompts import build_finops_advisor_prompt


class TokenControlTower:
    """
    Main entrypoint for Tokenomics SDK.

    Quickstart:
        tower = TokenControlTower(mode="dev")
        tower.track_turn(...)
        tower.profile()
        tower.launch_web(port=8080)
    """

    def __init__(
        self,
        mode: str | None = None,
        sink: str | None = None,
        project_id: str | None = None,
        dataset_id: str | None = None,
        table_id: str | None = None,
        view_id: str | None = None,
        default_model: str | None = None,
        models_config_path: str | None = None,
        custom_pricing: dict[str, dict[str, Any]] | None = None,
        optimization_rules: Any | None = None,
        optimization_prompts: dict[str, str] | None = None,
        prompts_config_path: str | None = None,
        legacy_flat_schema: bool = False,
    ):
        self.config = TokenomicsConfig(
            mode=mode,
            sink=sink,
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            view_id=view_id,
            default_model=default_model,
            models_config_path=models_config_path,
            legacy_flat_schema=legacy_flat_schema,
            custom_pricing=custom_pricing or {},
            prompts_config_path=prompts_config_path,
            optimization_rules=optimization_rules
            if optimization_rules is not None
            else OptimizationRulesConfig(),
            optimization_prompts=optimization_prompts or {},
        )
        self.sink: BaseSink = create_sink(self.config)
        self.tracker = TokenTracker(config=self.config, sink=self.sink)
        self.pricing = self.tracker.pricing

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
        """Registers a 3P (Claude, OpenAI), Open-Weight (Llama, DeepSeek, Mistral, Qwen, Ollama), or custom model."""
        entry = self.tracker.pricing.register_model(
            model_id=model_id,
            input_per_million=input_per_million,
            output_per_million=output_per_million,
            cached_per_million=cached_per_million,
            provider=provider,
            name=name,
            display_name=display_name,
            tier=tier,
        )
        cli_web_mod = sys.modules.get("adk_tokenomics_plugin.cli_web")
        web_handler = (
            getattr(cli_web_mod, "TokenomicsWebHandler", None)
            if cli_web_mod
            else None
        )
        web_pricing = (
            getattr(web_handler, "pricing", None) if web_handler else None
        )
        if web_pricing is not None and web_pricing is not self.tracker.pricing:
            try:
                web_pricing.register_model(
                    model_id=model_id,
                    input_per_million=input_per_million,
                    output_per_million=output_per_million,
                    cached_per_million=cached_per_million,
                    provider=provider,
                    name=name,
                    display_name=display_name,
                    tier=tier,
                )
            except Exception:
                pass
        return entry

    def create_adk_plugin(
        self,
        app_name: str = "customer_agent",
        auto_export_path: str | None = None,
    ) -> ADKTokenomicsPlugin:
        """Returns a drop-in Google ADK plugin for this control tower."""
        return ADKTokenomicsPlugin(
            tracker=self.tracker,
            app_name=app_name,
            auto_export_path=auto_export_path,
        )

    def set_optimization_prompt(self, rule_key: str, template: str):
        """Customizes a specific optimization recommendation template (e.g. 'context_caching', 'thinking_budget')."""
        self.config.optimization_prompts[str(rule_key)] = str(template)

    def configure_optimization_rules(self, **kwargs):
        """Updates heuristic thresholds on `self.config.optimization_rules` (e.g. `min_cache_tokens=2048`)."""
        unknown_keys = [
            k for k in kwargs if not hasattr(self.config.optimization_rules, k)
        ]
        if unknown_keys:
            raise ValueError(
                f"Unknown optimization rule key(s): {', '.join(sorted(unknown_keys))}"
            )
        updated = dataclasses.replace(self.config.optimization_rules, **kwargs)
        updated.validate()
        self.config.optimization_rules = updated

    def add_recommendation_rule(
        self, callback: Callable[[dict[str, Any]], str | None]
    ):
        """Registers a custom user-defined recommendation rule callback."""
        self.config.custom_recommendation_callbacks.append(callback)

    def get_finops_advisor_prompt(
        self,
        title: str = "Active Session",
        custom_system_prompt: str | None = None,
        custom_user_template: str | None = None,
    ) -> dict[str, str]:
        """Builds a `{system_instruction, user_prompt}` payload for AI-assisted FinOps analysis."""
        recent_turns = (
            list(self.tracker.turns_history[-50:])
            if self.tracker.turns_history
            else list(reversed(self.sink.fetch_recent(limit=50)))
        )
        return build_finops_advisor_prompt(
            turns=recent_turns,
            session_title=title,
            custom_system_prompt=custom_system_prompt,
            custom_user_template=custom_user_template,
            pricing_engine=self.tracker.pricing,
        )

    def track_turn(self, **kwargs) -> dict[str, Any]:
        """Convenience method to record a turn directly."""
        return self.tracker.record_turn(**kwargs)

    def get_summary(self) -> dict[str, Any]:
        """Returns live session metrics summary."""
        return self.tracker.get_summary()

    def profile(self, title: str = "Active Session") -> list[str]:
        """Prints a turn-by-turn context waterfall and optimization hints to stdout, and returns the recommendations."""
        from adk_tokenomics_plugin.cli_profiler import TokenProfiler

        profiler = TokenProfiler(
            pricing_engine=self.tracker.pricing,
            rules_config=self.config.optimization_rules,
            optimization_prompts=self.config.get_optimization_prompts(),
            custom_callbacks=self.config.custom_recommendation_callbacks,
        )
        if self.tracker.turns_history:
            recent_turns = list(self.tracker.turns_history[-50:])
        else:
            recent_turns = list(reversed(self.sink.fetch_recent(limit=50)))
        return profiler.profile_session(recent_turns, session_title=title)

    def launch_web(
        self,
        port: int = 8080,
        host: str = "127.0.0.1",
        open_browser: bool = True,
    ):
        """Launches the embedded Token Control Tower web dashboard."""
        from adk_tokenomics_plugin.cli_web import run_web_server

        run_web_server(
            port=port,
            host=host,
            project_id=self.config.project_id,
            dataset_id=self.config.dataset_id,
            table_id=self.config.table_id,
            view_id=self.config.view_id,
            open_browser=open_browser,
        )

    def launch_ui(
        self,
        port: int = 8080,
        host: str = "127.0.0.1",
        open_browser: bool = True,
    ):
        """Alias for launch_web."""
        self.launch_web(port=port, host=host, open_browser=open_browser)

    def provision_cloud_infra(self) -> bool:
        """Provisions production BigQuery partitioned dataset, table, and analytics view."""
        if not self.config.project_id:
            print(
                "[ERROR] GOOGLE_CLOUD_PROJECT must be set to provision BigQuery table.",
                flush=True,
            )
            return False
        return provision_bigquery_table(
            project_id=self.config.project_id,
            dataset_id=(
                self.config.dataset_id
                if self.config.dataset_id is not None
                else "bq_adk_ds"
            ),
            table_id=(
                self.config.table_id
                if self.config.table_id is not None
                else "adk_agent_events"
            ),
            view_id=(
                self.config.view_id
                if self.config.view_id is not None
                else "v_llm_response"
            ),
            legacy_flat_schema=self.config.legacy_flat_schema,
        )
