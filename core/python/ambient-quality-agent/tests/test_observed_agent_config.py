# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`ObservedAgentConfig` is a projection of `Config`, not a member of it, so
nothing in the type system keeps the two sides apart or keeps them in step.
These tests are what does.
"""

from __future__ import annotations

import dataclasses

import pytest
from ambient_quality_agent.config import (
    Config,
    Model,
    ObservedAgentConfig,
    extract_agent_config,
)

AQA_OWNED_FIELDS = frozenset(
    {
        "project_id",
        "location",
        "base_model",
        "insights_model",
        "verification_model",
        "insights_match_model",
        "selector_ai_model",
        "aqa_dataset",
        "aqa_dataset_location",
        "aqa_engine_id",
        "jobs_gcs_bucket",
        "source_gcs_bucket",
        "traces_gcs_bucket",
        "metrics_gcs_bucket",
        "content_capture_mode",
        "delay_task_queue",
        "ambient_caller_sa",
        "ambient_topic",
        "sync_investigation",
        "standalone",
        "pending_run_lease_minutes",
    }
)
"""The `Config` fields one AQuA deployment owns, whatever it is watching.

Spelled out rather than derived, so this file states the split independently of
the code it checks. `metrics_gcs_bucket` is here despite being per-agent in
spirit: its contents execute in the engine's process on the engine's
credentials, so write access to it is deploy access.
"""


def _field_names(cls: type) -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(cls))


def _config() -> Config:
    """Builds a test Config with all observed-agent fields set to non-default values.

    Returns:
        Config instance with customized agent settings for projection testing.
    """
    return Config(
        observed_agent_name="watched-agent",
        project_id="my-project",
        location="us-central1",
        telemetry_ingestion_source="cloud_ops",
        observed_project_id="agent-project",
        telemetry_dataset="custom_dataset",
        telemetry_table="custom_table",
        telemetry_location="US",
        quality_analysis_mode="eval_service",
        multi_turn_metrics=["task_success"],
        single_turn_metrics=["safety"],
        data_lookback_window=30,
        data_evaluation_cap=250,
        insights_auto_resolve_days=3,
        insights_verification_enabled=False,
        insights_verification_enforced=True,
        agent_revision_trigger_delay_seconds=300,
        base_model=Model(model="gemini-x", location="europe-west4"),
        jobs_gcs_bucket="jobs-bucket",
        metrics_gcs_bucket="metrics-bucket",
        pending_run_lease_minutes=30,
    )


def test_the_two_sides_partition_config_exactly() -> None:
    """A new `Config` field has to land on one side or the other.

    A projection cannot enforce that, so a field nobody assigned would silently
    stay AQuA-side. Failing here is the prompt to choose.
    """
    agent_fields = _field_names(ObservedAgentConfig)

    assert agent_fields | AQA_OWNED_FIELDS == _field_names(Config)
    assert not agent_fields & AQA_OWNED_FIELDS


def test_the_projection_mirrors_the_config_field_it_names() -> None:
    """Same type and same default on both sides. An `ObservedAgentConfig` built
    from scratch merges into a `Config` by name, so a drifted default would
    change a setting the caller never mentioned."""
    config_fields = {f.name: f for f in dataclasses.fields(Config)}

    for agent_field in dataclasses.fields(ObservedAgentConfig):
        mirrored = config_fields[agent_field.name]
        assert agent_field.type == mirrored.type, agent_field.name
        assert agent_field.default == mirrored.default, agent_field.name
        if agent_field.default_factory is not dataclasses.MISSING:
            assert mirrored.default_factory is not dataclasses.MISSING, (
                agent_field.name
            )
            assert (
                agent_field.default_factory() == mirrored.default_factory()
            ), agent_field.name


def test_projecting_and_merging_back_is_the_config_it_started_from() -> None:
    config = _config()

    assert config.copy_with_agent(extract_agent_config(config)) == config


def test_merging_leaves_every_aqa_field_alone() -> None:
    """`copy_with_agent` is how an attached agent's settings reach a run, so it must
    not be able to repoint AQuA's own models, datasets or buckets."""
    config = _config()

    merged = config.copy_with_agent(
        ObservedAgentConfig(observed_agent_name="other-agent")
    )

    for name in AQA_OWNED_FIELDS:
        assert getattr(merged, name) == getattr(config, name), name


def test_merging_replaces_every_observed_agent_field() -> None:
    """Including the ones left at their default: the agent config is the whole
    answer for its side, not a patch over whatever the environment said."""
    config = _config()
    default_agent = ObservedAgentConfig(observed_agent_name="other-agent")

    merged = config.copy_with_agent(default_agent)

    assert extract_agent_config(merged) == default_agent
    assert merged.observed_agent_name == "other-agent"
    assert merged.data_lookback_window == 7
    assert merged.telemetry_dataset == "agent_analytics"


def test_a_merged_value_is_validated_like_an_environment_one() -> None:
    """Validation lives in `Config.__post_init__` alone, so the merge is where a
    value from outside the process is clamped, filtered and rejected."""
    config = _config()

    merged = config.copy_with_agent(
        ObservedAgentConfig(
            observed_agent_name="other-agent",
            data_lookback_window=9999,
            multi_turn_metrics=["task_success", "not_a_metric"],
        )
    )

    assert merged.data_lookback_window == 64
    assert merged.multi_turn_metrics == ["task_success"]

    with pytest.raises(
        ValueError, match="quality_analysis_mode must be one of"
    ):
        config.copy_with_agent(
            ObservedAgentConfig(
                observed_agent_name="other-agent",
                quality_analysis_mode="nonsense",
            )
        )


def test_telemetry_is_in_aquas_own_project_unless_the_agent_names_another() -> (
    None
):
    config = _config()

    assert config.resolve_observed_project_id() == "agent-project"
    assert (
        dataclasses.replace(
            config, observed_project_id=""
        ).resolve_observed_project_id()
        == "my-project"
    )
