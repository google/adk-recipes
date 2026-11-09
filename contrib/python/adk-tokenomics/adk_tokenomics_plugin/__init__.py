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
Tokenomics SDK & Token Control Tower Framework
Turnkey Token Economics, Telemetry, Context Optimization & FinOps for AI Agents.
"""

__version__ = "1.0.2"

from adk_tokenomics_plugin.core.config import (
    OptimizationRulesConfig,
    TokenomicsConfig,
    canonicalize_model_id,
    resolve_env_bq_identifiers,
    resolve_models_config_path,
    resolve_writable_models_config_path,
    validate_bq_identifier,
    validate_bq_project_id,
)
from adk_tokenomics_plugin.core.pricing import (
    PricingEngine,
    normalize_turn_tokens,
)
from adk_tokenomics_plugin.core.sinks import (
    BaseSink,
    BigQuerySink,
    InMemorySink,
    create_sink,
    parse_bq_turn_record,
)
from adk_tokenomics_plugin.core.tracker import TokenTracker
from adk_tokenomics_plugin.finops.aggregator import FinOpsAggregator
from adk_tokenomics_plugin.finops.bq_schema import (
    ADK_AGENT_EVENTS_DDL,
    ADK_LLM_RESPONSE_VIEW_DDL,
    BIGQUERY_TABLE_DDL,
    provision_bigquery_table,
)
from adk_tokenomics_plugin.middleware.adk_callback import ADKTokenomicsPlugin
from adk_tokenomics_plugin.middleware.decorator import track_tokens
from adk_tokenomics_plugin.prompts import (
    DEFAULT_OPTIMIZATION_PROMPTS,
    FINOPS_ADVISOR_SYSTEM_PROMPT,
    FINOPS_ADVISOR_USER_TEMPLATE,
    build_finops_advisor_prompt,
    get_optimization_prompts,
)
from adk_tokenomics_plugin.testing.assertions import (
    assert_cache_ratio_above,
    assert_cost_under,
    assert_thinking_budget,
    assert_token_budget,
)
from adk_tokenomics_plugin.testing.test_case import TokenomicsTestCase
from adk_tokenomics_plugin.tower import TokenControlTower

try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover
    pass

__all__ = [
    "ADK_AGENT_EVENTS_DDL",
    "ADK_LLM_RESPONSE_VIEW_DDL",
    "BIGQUERY_TABLE_DDL",
    "DEFAULT_OPTIMIZATION_PROMPTS",
    "FINOPS_ADVISOR_SYSTEM_PROMPT",
    "FINOPS_ADVISOR_USER_TEMPLATE",
    "ADKTokenomicsPlugin",
    "BaseSink",
    "BigQuerySink",
    "FinOpsAggregator",
    "InMemorySink",
    "OptimizationRulesConfig",
    "PricingEngine",
    "TokenControlTower",
    "TokenTracker",
    "TokenomicsConfig",
    "TokenomicsTestCase",
    "assert_cache_ratio_above",
    "assert_cost_under",
    "assert_thinking_budget",
    "assert_token_budget",
    "build_finops_advisor_prompt",
    "canonicalize_model_id",
    "create_sink",
    "get_optimization_prompts",
    "normalize_turn_tokens",
    "parse_bq_turn_record",
    "provision_bigquery_table",
    "resolve_env_bq_identifiers",
    "resolve_models_config_path",
    "resolve_writable_models_config_path",
    "track_tokens",
    "validate_bq_identifier",
    "validate_bq_project_id",
]
