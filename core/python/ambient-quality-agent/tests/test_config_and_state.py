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

"""Tests for Config loading and the WorkflowState model."""

from __future__ import annotations

import dataclasses

import pytest
from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import (
    DEFAULT_BIGQUERY_LOCATION,
    Config,
)
from ambient_quality_agent.core.state import WorkflowState

from .conftest import make_config

# The env vars without defaults that `load()` requires.
_REQUIRED_ENV = {
    "GOOGLE_CLOUD_PROJECT": "my-project",
    "GOOGLE_CLOUD_LOCATION": "us-central1",
}


_AQA_ENV_VARS = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "AQA_DATASET_LOCATION",
    "AQA_BASE_MODEL",
    "AQA_BASE_LOCATION",
    "AQA_INSIGHTS_MODEL",
    "AQA_INSIGHTS_LOCATION",
    "GOOGLE_CLOUD_AGENT_ENGINE_ID",
    "AQA_JOBS_GCS_BUCKET",
    "AQA_SOURCE_GCS_BUCKET",
    "AQA_PENDING_RUN_LEASE_MINUTES",
    "AQA_DELAY_TASK_QUEUE",
    "AQA_AMBIENT_CALLER_SA",
    "AQA_SELECTOR_AI_MODEL",
)

_CAPTURE_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes all AQA-related env vars so a test starts from a clean slate.

    Clearing ensures each test only sees explicitly configured environment variables.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    for var in _AQA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_load_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only required vars set, defaults are applied."""
    _set_required(monkeypatch)

    cfg = config_module.load()

    assert cfg.observed_agent_name == ""
    assert cfg.project_id == "my-project"
    assert cfg.telemetry_ingestion_source == "big_query"
    # The default source is big_query, so its dataset/table defaults apply.
    assert cfg.telemetry_dataset == "agent_analytics"
    assert cfg.telemetry_table == "agent_events"
    assert cfg.telemetry_location == DEFAULT_BIGQUERY_LOCATION
    # AQuA's own dataset defaults to `location` when unset.
    assert cfg.aqa_dataset_location == cfg.location == "us-central1"
    assert cfg.data_evaluation_cap == 1000
    assert cfg.multi_turn_metrics == [
        "task_success",
        "tool_use_quality",
        "trajectory_quality",
    ]
    assert cfg.single_turn_metrics == []
    assert cfg.data_lookback_window == 7
    assert cfg.base_model == config_module.Model(
        model="gemini-3.8-flash", location="global"
    )
    assert cfg.aqa_engine_id == ""
    assert cfg.jobs_gcs_bucket == ""
    assert cfg.source_gcs_bucket == ""
    # Ambient triggering: long enough for the new revision to have traces.
    assert cfg.agent_revision_trigger_delay_seconds == 900


def test_load_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """AQuA's own env vars override their defaults."""
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_BASE_MODEL", "gemini-3.0-pro")
    monkeypatch.setenv("AQA_BASE_LOCATION", "europe-west4")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "98765")
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "my-bucket")
    monkeypatch.setenv("AQA_SOURCE_GCS_BUCKET", "my-source-bucket")
    monkeypatch.setenv("AQA_DATASET_LOCATION", "US")

    cfg = config_module.load()

    assert cfg.base_model == config_module.Model(
        model="gemini-3.0-pro", location="europe-west4"
    )
    assert cfg.aqa_engine_id == "98765"
    assert cfg.jobs_gcs_bucket == "my-bucket"
    assert cfg.source_gcs_bucket == "my-source-bucket"
    assert cfg.aqa_dataset_location == "US"
    assert cfg.location == "us-central1"


def test_the_selector_ai_model_defaults_to_the_built_in_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selector AI falls back to DEFAULT_SELECTOR_AI_MODEL when unset."""
    _set_required(monkeypatch)

    cfg = config_module.load()

    assert cfg.selector_ai_model == config_module.DEFAULT_SELECTOR_AI_MODEL


def test_the_selector_ai_model_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selector AI loads its model override from the environment."""
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_SELECTOR_AI_MODEL", "gemini-x")

    cfg = config_module.load()

    assert cfg.selector_ai_model == "gemini-x"


def test_insights_model_location_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)

    cfg = config_module.load()

    assert cfg.insights_model == config_module.Model(
        model=config_module.DEFAULT_INSIGHTS_MODEL, location="global"
    )


def test_insights_model_location_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_INSIGHTS_MODEL", "gemini-x")
    monkeypatch.setenv("AQA_INSIGHTS_LOCATION", "asia-east1")

    cfg = config_module.load()

    assert cfg.insights_model == config_module.Model(
        model="gemini-x", location="asia-east1"
    )


@pytest.mark.parametrize(
    ("field", "env_prefix", "default_model"),
    [
        ("base_model", "AQA_BASE", "gemini-3.8-flash"),
        (
            "insights_model",
            "AQA_INSIGHTS",
            config_module.DEFAULT_INSIGHTS_MODEL,
        ),
    ],
)
def test_model_group_independent_from_env(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    env_prefix: str,
    default_model: str,
) -> None:
    """Each model group reads its own *_MODEL / *_LOCATION independently."""
    _set_required(monkeypatch)
    monkeypatch.setenv(f"{env_prefix}_MODEL", f"model-{field}")
    monkeypatch.setenv(f"{env_prefix}_LOCATION", f"loc-{field}")

    cfg = config_module.load()

    assert getattr(cfg, field) == config_module.Model(
        model=f"model-{field}", location=f"loc-{field}"
    )
    # Other groups keep their defaults; only this one changed.
    defaults = {
        "base_model": "gemini-3.8-flash",
        "insights_model": config_module.DEFAULT_INSIGHTS_MODEL,
    }
    for other, other_default in defaults.items():
        if other != field:
            assert getattr(cfg, other) == config_module.Model(
                model=other_default, location="global"
            )


def test_model_dict_roundtrip_coercion() -> None:
    """asdict()->Config(**fields) rebuilds nested Models from plain dicts."""

    cfg = config_module.Config(
        observed_agent_name="a",
        project_id="p",
        location="us-central1",
        base_model=config_module.Model(model="m", location="l"),
        jobs_gcs_bucket="jobs-bucket",
        source_gcs_bucket="source-bucket",
    )
    rebuilt = config_module.Config(**dataclasses.asdict(cfg))

    assert isinstance(rebuilt.base_model, config_module.Model)
    assert rebuilt.base_model == cfg.base_model
    assert rebuilt.jobs_gcs_bucket == "jobs-bucket"
    assert rebuilt.source_gcs_bucket == "source-bucket"


def test_load_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing required env var raises a clear error."""
    _clean_env(monkeypatch)

    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        config_module.load()


def test_the_environment_configures_no_observed_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent is configured by its attachment alone, so settings named after
    an agent's fields leave every one of them at its default."""
    _set_required(monkeypatch)
    for var, value in {
        "AQA_OBSERVED_AGENT_NAME": "watched-agent",
        "TELEMETRY_INGESTION_SOURCE": "cloud_ops",
        "AQA_TELEMETRY_DATASET": "custom_dataset",
        "DATA_LOOKBACK_WINDOW": "30",
        "MULTI_TURN_METRICS": "task_success",
        "AQA_INSIGHTS_VERIFICATION_ENABLED": "false",
    }.items():
        monkeypatch.setenv(var, value)

    cfg = config_module.load()

    assert config_module.extract_agent_config(
        cfg
    ) == config_module.ObservedAgentConfig(observed_agent_name="")


def test_invalid_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer value for an int var raises a clear error."""
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_PENDING_RUN_LEASE_MINUTES", "not-a-number")

    with pytest.raises(RuntimeError, match="AQA_PENDING_RUN_LEASE_MINUTES"):
        config_module.load()


def test_content_capture_mode_reports_the_otel_setting_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies content_capture_mode mirrors the OpenTelemetry capture environment variable."""
    _set_required(monkeypatch)
    monkeypatch.delenv(_CAPTURE_VAR, raising=False)

    assert config_module.load().content_capture_mode == ""

    monkeypatch.setenv(_CAPTURE_VAR, "SPAN_ONLY")
    assert config_module.load().content_capture_mode == "SPAN_ONLY"


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config instances are immutable."""
    _set_required(monkeypatch)
    cfg = config_module.load()

    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        setattr(cfg, "project_id", "other")  # noqa: B010 - assignment rejected by type checker


def test_invalid_telemetry_source_raises() -> None:
    """An unsupported telemetry source is rejected."""
    with pytest.raises(ValueError, match="telemetry_ingestion_source"):
        make_config(telemetry_ingestion_source="spanner")


def test_quality_analysis_mode_defaults_to_session_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure unconfigured deployments default to session_review mode."""
    _set_required(monkeypatch)

    assert config_module.load().quality_analysis_mode == "session_review"


def test_invalid_quality_analysis_mode_raises() -> None:
    """Verify invalid quality_analysis_mode values fail validation."""
    with pytest.raises(ValueError, match="quality_analysis_mode"):
        make_config(quality_analysis_mode="vibes")


def test_the_dataset_location_does_not_move_the_telemetry_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The telemetry read and the agent's own tables can sit in different regions."""
    _set_required(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("AQA_DATASET_LOCATION", "europe-west1")

    cfg = config_module.load()

    assert cfg.telemetry_location == DEFAULT_BIGQUERY_LOCATION
    assert cfg.aqa_dataset_location == "europe-west1"


def test_data_evaluation_cap_clamped_to_max(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cap above the max is clamped to 1000 with a warning."""
    with caplog.at_level("WARNING"):
        cfg = make_config(data_evaluation_cap=5000)

    assert cfg.data_evaluation_cap == 1000
    assert "data_evaluation_cap" in caplog.text


@pytest.mark.parametrize(
    "days", [365, 9999, config_module.MAX_DATA_LOOKBACK_WINDOW]
)
def test_a_long_data_lookback_window_is_kept(
    caplog: pytest.LogCaptureFixture, days: int
) -> None:
    with caplog.at_level("WARNING"):
        cfg = make_config(data_lookback_window=days)

    assert cfg.data_lookback_window == days
    assert "data_lookback_window" not in caplog.text


@pytest.mark.parametrize(
    "days", [config_module.MAX_DATA_LOOKBACK_WINDOW + 1, 999999]
)
def test_data_lookback_window_clamped_to_max(
    caplog: pytest.LogCaptureFixture, days: int
) -> None:
    with caplog.at_level("WARNING"):
        cfg = make_config(data_lookback_window=days)

    assert cfg.data_lookback_window == 36500
    assert "data_lookback_window" in caplog.text


def test_numeric_minimum_clamped_to_one() -> None:
    """Values below 1 are clamped up to 1."""
    cfg = make_config(data_evaluation_cap=0, data_lookback_window=-3)

    assert cfg.data_evaluation_cap == 1
    assert cfg.data_lookback_window == 1


@pytest.mark.parametrize(
    ("mode", "setting", "expected"),
    [
        ("eval_service", None, True),
        ("session_review", None, True),
        ("eval_service", False, False),
        ("session_review", False, False),
        ("session_review", True, True),
    ],
)
def test_verification_is_on_under_every_mode_and_yields_to_the_setting(
    mode: str,
    setting: bool | None,
    expected: bool,
) -> None:
    """The pass writes the description and the label a reader acts on, so a
    sweep without it produces insights nobody can triage. It is on under every
    mode. An explicit value still wins, the false one included -- that is what
    "unset" must not collapse into, and it is the switch an attachment needs to
    take the pass out while something else is investigated."""
    cfg = make_config(
        quality_analysis_mode=mode, insights_verification_enabled=setting
    )

    assert (
        config_module.is_verification_enabled(cfg.insights_verification_enabled)
        is expected
    )


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        (None, False),
        (True, True),
        (False, False),
    ],
)
def test_the_verdict_gates_nothing_until_the_setting_says_it_does(
    setting: bool | None,
    expected: bool,
) -> None:
    """An enforced pass drops a real issue whenever a trace omits the tool error
    the defect turns on, so the gate is off until an operator asks for it: unset
    has to read as off, and only an explicit setting may turn it on."""
    cfg = make_config(insights_verification_enforced=setting)

    assert (
        config_module.is_verification_enforced(
            cfg.insights_verification_enforced
        )
        is expected
    )


@pytest.mark.parametrize("setting", [None, True, False])
def test_the_verification_setting_survives_a_config_roundtrip(
    setting: bool | None,
) -> None:
    """`effective_config.load` and the durable-run boundary both rebuild a
    `Config` from its own `asdict`. An unset flag has to come back unset, or the
    mode it defers to could no longer change it; an explicit one has to come
    back as itself, or an override would not survive being carried."""
    cfg = config_module.Config(
        observed_agent_name="a",
        project_id="p",
        location="us-central1",
        insights_verification_enabled=setting,
        insights_verification_enforced=setting,
    )

    rebuilt = config_module.Config(**dataclasses.asdict(cfg))

    assert rebuilt.insights_verification_enabled is setting
    assert rebuilt.insights_verification_enforced is setting


def test_agent_revision_trigger_delay_negative_raises() -> None:
    """A negative revision-trigger delay fails fast rather than clamping to 0."""
    with pytest.raises(
        ValueError, match="agent_revision_trigger_delay_seconds must be >= 0"
    ):
        make_config(agent_revision_trigger_delay_seconds=-5)


def test_ambient_update_path_queue_without_sa_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue with no caller SA is partial wiring and fails fast (Max review [3])."""
    _set_required(monkeypatch)
    monkeypatch.setenv(
        "AQA_DELAY_TASK_QUEUE", "projects/p/locations/l/queues/q"
    )

    with pytest.raises(ValueError, match="partial ambient update-path config"):
        config_module.load()


def test_ambient_update_path_sa_without_queue_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller SA with no queue is partial wiring and fails fast (the reverse)."""
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_AMBIENT_CALLER_SA", "sa@p.iam.gserviceaccount.com")

    with pytest.raises(ValueError, match="partial ambient update-path config"):
        config_module.load()


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="all-unset-non-ambient"),
        pytest.param(
            {
                "AQA_DELAY_TASK_QUEUE": "projects/p/locations/l/queues/q",
                "AQA_AMBIENT_CALLER_SA": "sa@p.iam.gserviceaccount.com",
            },
            id="update-path-wired",
        ),
    ],
)
def test_ambient_valid_wiring_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    """Every legitimate ambient wiring loads without raising.

    Fail-fast rejects only clearly-broken config; a fully-absent or
    fully-present update path is valid.
    """
    _set_required(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    config_module.load()  # must not raise


def test_unknown_metrics_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Metrics outside the allowed set are dropped with a warning."""
    with caplog.at_level("WARNING"):
        cfg = make_config(
            multi_turn_metrics=["task_success", "bogus_metric"],
            single_turn_metrics=["safety", "not_a_metric"],
        )

    assert cfg.multi_turn_metrics == ["task_success"]
    assert cfg.single_turn_metrics == ["safety"]
    assert "bogus_metric" in caplog.text
    assert "not_a_metric" in caplog.text


def test_duplicate_metrics_deduplicated() -> None:
    """Repeated valid metrics are collapsed to a single entry, order kept."""
    cfg = make_config(
        multi_turn_metrics=["task_success", "task_success", "tool_use_quality"]
    )

    assert cfg.multi_turn_metrics == ["task_success", "tool_use_quality"]


def test_single_turn_allowed_metrics() -> None:
    """All four single-turn metrics are accepted."""
    metrics = [
        "final_response_quality",
        "hallucination",
        "tool_use_quality",
        "safety",
    ]

    cfg = make_config(single_turn_metrics=metrics)

    assert cfg.single_turn_metrics == metrics


def test_bigquery_location_explicit_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit location env var beats the agent location."""
    monkeypatch.setenv("AQA_DATASET_LOCATION", "europe-west1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "asia-east1")

    resolved = config_module._resolve_bigquery_location("AQA_DATASET_LOCATION")
    assert resolved == "europe-west1"


def test_bigquery_location_falls_back_to_agent_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit override, GOOGLE_CLOUD_LOCATION is used."""
    monkeypatch.delenv("AQA_DATASET_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")

    resolved = config_module._resolve_bigquery_location("AQA_DATASET_LOCATION")
    assert resolved == "europe-west4"


def test_bigquery_location_ignores_global_agent_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "global" location is skipped (BigQuery has no global)."""
    monkeypatch.delenv("AQA_DATASET_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    resolved = config_module._resolve_bigquery_location("AQA_DATASET_LOCATION")
    assert resolved == DEFAULT_BIGQUERY_LOCATION


def test_bigquery_location_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing configured, the local default applies."""
    monkeypatch.delenv("AQA_DATASET_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    resolved = config_module._resolve_bigquery_location("AQA_DATASET_LOCATION")
    assert resolved == DEFAULT_BIGQUERY_LOCATION


def test_load_resolves_the_aqa_dataset_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load()` populates AQuA's dataset location via the resolver."""
    _set_required(monkeypatch)
    monkeypatch.setenv("AQA_DATASET_LOCATION", "US")

    cfg = config_module.load()

    assert cfg.aqa_dataset_location == "US"


def test_load_bigquery_location_defaults_to_agent_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit override, `load()` falls back to the agent region."""
    _set_required(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")

    cfg = config_module.load()

    assert cfg.aqa_dataset_location == "europe-west4"


def test_workflow_state_defaults() -> None:
    """budget_per_metric defaults to 0 and metric lists default correctly."""
    state = WorkflowState(
        observed_agent_name="watched-agent",
        project_id="my-project",
        location="us-central1",
    )

    assert state.budget_per_metric == 0
    assert state.data_evaluation_cap == 1000
    assert state.multi_turn_metrics == [
        "task_success",
        "tool_use_quality",
        "trajectory_quality",
    ]
    assert state.single_turn_metrics == []
    assert state.data_lookback_window == 7


def test_workflow_state_from_config() -> None:
    """from_config copies every configuration input across."""
    cfg: Config = make_config(
        observed_agent_name="watched-agent",
        single_turn_metrics=["safety"],
        # Set away from its default: this copy is the only path by which the
        # flag reaches `insight_correlation`, which reads it off the state and
        # not off the config.
        insights_verification_enabled=True,
        insights_verification_enforced=True,
        # The review reads the goal from here, so a local run without it would
        # review with no goal.
        jobs_gcs_bucket="aqa-jobs",
    )

    state = WorkflowState.from_config(cfg)

    assert state.observed_agent_name == "watched-agent"
    assert state.jobs_gcs_bucket == "aqa-jobs"
    assert state.project_id == cfg.project_id
    assert state.location == cfg.location
    assert state.quality_analysis_mode == cfg.quality_analysis_mode
    assert state.telemetry_ingestion_source == cfg.telemetry_ingestion_source
    assert state.telemetry_dataset == cfg.telemetry_dataset
    assert state.telemetry_table == cfg.telemetry_table
    assert state.telemetry_location == cfg.telemetry_location
    assert state.aqa_dataset_location == cfg.aqa_dataset_location
    assert state.data_evaluation_cap == cfg.data_evaluation_cap
    assert state.multi_turn_metrics == cfg.multi_turn_metrics
    assert state.single_turn_metrics == ["safety"]
    assert state.data_lookback_window == cfg.data_lookback_window
    assert state.insights_verification_enabled is True
    assert state.insights_verification_enforced is True
    # Model groups mirror across.
    assert state.base_model == cfg.base_model
    # Calculated field keeps its default until a node populates it.
    assert state.budget_per_metric == 0


def test_workflow_state_is_mutable() -> None:
    """Nodes mutate state in place — budget_per_metric is assignable."""
    state = WorkflowState(
        observed_agent_name="watched-agent",
        project_id="my-project",
        location="us-central1",
    )

    state.budget_per_metric = 42

    assert state.budget_per_metric == 42


# --- the agent key AQuA's rows are written under ---------------------------- #


def test_rows_are_keyed_by_the_agent_key_when_there_is_one() -> None:
    cfg = make_config(observed_agent_name="bot", agent_key="acme-prod__bot")

    assert cfg.resolve_agent_key() == "acme-prod__bot"
    assert config_module.extract_agent_key(dataclasses.asdict(cfg)) == (
        "acme-prod__bot"
    )


def test_rows_fall_back_to_the_agent_name_without_a_key() -> None:
    """A run snapshot taken without a key."""
    cfg = make_config(observed_agent_name="bot")

    assert cfg.resolve_agent_key() == "bot"
    assert config_module.extract_agent_key({"observed_agent_name": "bot"}) == (
        "bot"
    )


def test_the_workflow_state_carries_the_agent_key() -> None:
    cfg = make_config(observed_agent_name="bot", agent_key="acme-prod__bot")

    state = WorkflowState.from_config(cfg)

    assert state.agent_key == "acme-prod__bot"
    assert state.observed_agent_name == "bot"
