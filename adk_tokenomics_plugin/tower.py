"""
Tokenomics SDK - TokenControlTower Main Hub
High-level plug-and-play interface for connecting Tokenomics to any AI Agent lifecycle.
"""

import dataclasses
import sys
from typing import Any, Callable, Dict, List, Optional

from adk_tokenomics_plugin.core.config import OptimizationRulesConfig, TokenomicsConfig
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
        mode: Optional[str] = None,
        sink: Optional[str] = None,
        project_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        table_id: Optional[str] = None,
        view_id: Optional[str] = None,
        default_model: Optional[str] = None,
        models_config_path: Optional[str] = None,
        custom_pricing: Optional[Dict[str, Dict[str, Any]]] = None,
        optimization_rules: Optional[Any] = None,
        optimization_prompts: Optional[Dict[str, str]] = None,
        prompts_config_path: Optional[str] = None,
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
            optimization_rules=optimization_rules if optimization_rules is not None else OptimizationRulesConfig(),
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
        cached_per_million: Optional[float] = None,
        provider: str = "custom",
        name: Optional[str] = None,
        display_name: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
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
        web_handler = getattr(cli_web_mod, "TokenomicsWebHandler", None) if cli_web_mod else None
        web_pricing = getattr(web_handler, "pricing", None) if web_handler else None
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
        auto_export_path: Optional[str] = None,
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
        unknown_keys = [k for k in kwargs if not hasattr(self.config.optimization_rules, k)]
        if unknown_keys:
            raise ValueError(f"Unknown optimization rule key(s): {', '.join(sorted(unknown_keys))}")
        updated = dataclasses.replace(self.config.optimization_rules, **kwargs)
        updated.validate()
        self.config.optimization_rules = updated

    def add_recommendation_rule(self, callback: Callable[[Dict[str, Any]], Optional[str]]):
        """Registers a custom user-defined recommendation rule callback."""
        self.config.custom_recommendation_callbacks.append(callback)

    def get_finops_advisor_prompt(
        self,
        title: str = "Active Session",
        custom_system_prompt: Optional[str] = None,
        custom_user_template: Optional[str] = None,
    ) -> Dict[str, str]:
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

    def track_turn(self, **kwargs) -> Dict[str, Any]:
        """Convenience method to record a turn directly."""
        return self.tracker.record_turn(**kwargs)

    def get_summary(self) -> Dict[str, Any]:
        """Returns live session metrics summary."""
        return self.tracker.get_summary()

    def profile(self, title: str = "Active Session") -> List[str]:
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

    def launch_web(self, port: int = 8080, host: str = "127.0.0.1", open_browser: bool = True):
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

    def launch_ui(self, port: int = 8080, host: str = "127.0.0.1", open_browser: bool = True):
        """Alias for launch_web."""
        self.launch_web(port=port, host=host, open_browser=open_browser)

    def provision_cloud_infra(self) -> bool:
        """Provisions production BigQuery partitioned dataset, table, and analytics view."""
        if not self.config.project_id:
            print("[ERROR] GOOGLE_CLOUD_PROJECT must be set to provision BigQuery table.", flush=True)
            return False
        return provision_bigquery_table(
            project_id=self.config.project_id,
            dataset_id=self.config.dataset_id,
            table_id=self.config.table_id,
            view_id=self.config.view_id,
            legacy_flat_schema=self.config.legacy_flat_schema,
        )
