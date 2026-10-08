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

"""Process-wide configuration loaded once at startup.

All AQA runtime parameters that come from the environment (set by
agents-cli at deploy time, or by the developer for local runs) live
here. The module exports a single frozen `config` object that the
rest of the codebase imports.

Add new fields here as new env vars are introduced. A field belongs either to
AQuA's own deployment or to the agent being observed; the latter are also named
by `ObservedAgentConfig`, and a test fails until a new field lands on one side.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
from collections.abc import Collection
from dataclasses import dataclass, field

from ambient_quality_agent.tools.evaluation.models import (
    MULTI_TURN_METRICS,
    SINGLE_TURN_METRICS,
)

logger = logging.getLogger(__name__)

# Allowed values / limits. These are deploy-time constraints: config is
# loaded once at process startup (see `config` at the bottom of this
# module) and stays fixed until the agent is redeployed.
ALLOWED_TELEMETRY_INGESTION_SOURCES = frozenset(
    {"big_query", "cloud_ops", "cloud_logging"}
)
"""Supported telemetry sources: the ADK BigQuery analytics plugin
(``big_query``), the Cloud Trace linked dataset (``cloud_ops``), and the
Cloud Logging GenAI export sink table (``cloud_logging``)."""

ALLOWED_QUALITY_ANALYSIS_MODES = frozenset({"session_review", "eval_service"})
"""Supported quality analysis modes: LLM review of each session
(``session_review``) or eval metrics in Gemini platform (``eval_service``).

Code metrics are not a mode. Publishing one to `metrics_gcs_bucket` adds it to
the session-review sweep, and publishing none leaves that sweep unchanged."""

DEFAULT_QUALITY_ANALYSIS_MODE = "session_review"

"""Default quality analysis mode."""

DEFAULT_BQ_LOGGING_TABLE = "gen_ai_client_inference_operation_details"
"""Default table name for the Cloud Logging GenAI export sink (the
``gen_ai.client.inference.operation.details`` log routed to BigQuery)."""

DEFAULT_TELEMETRY_DATASETS = {
    "big_query": "agent_analytics",
    "cloud_ops": "",
    "cloud_logging": "",
}
"""Default `telemetry_dataset` per source."""

DEFAULT_TELEMETRY_TABLES = {
    "big_query": "agent_events",
    "cloud_ops": "_AllSpans",
    "cloud_logging": DEFAULT_BQ_LOGGING_TABLE,
}
"""Default `telemetry_table` per source."""

MAX_DATA_EVALUATION_CAP = 1000
"""Upper bound (and default) for `data_evaluation_cap`."""

MAX_DATA_LOOKBACK_WINDOW = 64
"""Upper bound for `data_lookback_window`, in days."""

DEFAULT_AGENT_REVISION_TRIGGER_DELAY_SECONDS = 900
"""Default wait after an observed-agent update before investigating, so the
new revision's traces have time to accumulate."""

DEFAULT_BIGQUERY_LOCATION = "us-central1"
"""Fallback BigQuery location used when the agent's region is not
available from the environment (e.g. local runs)."""

DEFAULT_LLM_LOCATION = "global"
"""Default serving location for LLM roles, independent of
``GOOGLE_CLOUD_LOCATION``."""

DEFAULT_ENGINE_LOCATION = "us-central1"
"""Fallback region for the agent's own engine. An engine is regional and its
query-job API answers 501 outside a region, so ``global`` is never usable
here."""

DEFAULT_BASE_MODEL = "gemini-3.8-flash"
"""Default model for the orchestrator and the dashboard chat agent."""

DEFAULT_INSIGHTS_MODEL = "gemini-3.1-pro-preview"
"""Default model for failed-rubric clustering."""

DEFAULT_INSIGHTS_MATCH_MODEL = "gemini-3.5-flash-lite"
"""Default model for the same-issue judge in insight matching."""

DEFAULT_VERIFICATION_MODEL = "gemini-3.7-flash"
"""Default model for the cluster verification pass. Held apart from
`DEFAULT_INSIGHTS_MODEL`, whose four other callers are tuned against it."""

DEFAULT_SELECTOR_AI_MODEL = "gemini-3.5-flash-lite"
"""Default model for the `AI.IF` predicate in conversation selectors. Uses a
lightweight model to optimize cost and latency across row scans."""


@dataclass(frozen=True)
class Model:
    """A Gemini model bound to its own serving location."""

    model: str = DEFAULT_BASE_MODEL
    location: str = DEFAULT_LLM_LOCATION


@dataclass(frozen=True)
class Config:
    """Resolved AQA configuration."""

    observed_agent_name: str
    """Name of the watched agent — matches `Agent(name=...)` in the
    observed agent's code. Set by agents-cli via
    `AQA_OBSERVED_AGENT_NAME` at deploy time. Empty in a deployment that names
    no agent, where `effective_config` takes it from the default attached
    agent."""

    project_id: str
    """Google Cloud project that hosts the watched agent. Set via
    the standard `GOOGLE_CLOUD_PROJECT` env var."""

    location: str
    """Google Cloud region for the evaluation client and the Agent Runtime agent
    (set via `GOOGLE_CLOUD_LOCATION`). Also the BigQuery-location fallback."""

    base_model: Model = field(default_factory=Model)
    """Base model."""

    insights_model: Model = field(
        default_factory=lambda: Model(model=DEFAULT_INSIGHTS_MODEL)
    )
    """Failed-rubric clustering model. Set via `AQA_INSIGHTS_MODEL` /
    `AQA_INSIGHTS_LOCATION`."""

    verification_model: Model = field(
        default_factory=lambda: Model(model=DEFAULT_VERIFICATION_MODEL)
    )
    """Cluster verification model. Set via `AQA_VERIFICATION_MODEL` /
    `AQA_VERIFICATION_LOCATION`."""

    insights_match_model: str = DEFAULT_INSIGHTS_MATCH_MODEL
    """Model name for the same-issue judge in insight matching. Serves from
    `DEFAULT_LLM_LOCATION`, so only the name is configurable, via
    `AQA_INSIGHTS_MATCH_MODEL`."""

    selector_ai_model: str = DEFAULT_SELECTOR_AI_MODEL
    """Model invoked by `AI.IF` in conversation selectors. Model calls execute
    under the query executor's credentials (`roles/aiplatform.user`). Set via
    `AQA_SELECTOR_AI_MODEL`."""

    aqa_dataset: str = "aqua_insights"
    """BigQuery dataset the agent owns for its own tables. Set via
    `AQA_DATASET`; its location is `aqa_dataset_location`."""

    aqa_dataset_location: str = DEFAULT_BIGQUERY_LOCATION
    """BigQuery location of `aqa_dataset`. Set via `AQA_DATASET_LOCATION`,
    resolved with the same region precedence as `telemetry_location`."""

    insights_auto_resolve_days: int = 14
    """Resolve an insight after this many days without a sighting; `0` disables
    auto-resolution. Set via `AQA_INSIGHTS_AUTO_RESOLVE_DAYS`."""

    insights_verification_enabled: bool | None = None
    """Whether to check each candidate issue against its own traces before it
    becomes an insight -- one model call per candidate, and the sweep's most
    expensive step. ``None``, the default, means on, and is preserved as-is
    through every override and round trip so an explicit ``False`` stays
    distinguishable from it. Set via `AQA_INSIGHTS_VERIFICATION_ENABLED`."""

    insights_verification_enforced: bool | None = None
    """Whether a negative verdict withholds the candidate rather than being
    recorded against it. ``None``, the default, means off, and is preserved
    as-is on the same terms as `insights_verification_enabled`. Set via
    `AQA_INSIGHTS_VERIFICATION_ENFORCED`."""

    aqa_engine_id: str = ""
    """AQA's *own* reasoning-engine id, used to submit the durable self-query.
    Auto-injected by Agent Runtime via `GOOGLE_CLOUD_AGENT_ENGINE_ID`; empty
    when running locally."""

    jobs_gcs_bucket: str = ""
    """GCS bucket for durable long-running query job I/O. Set via
    `AQA_JOBS_GCS_BUCKET`."""

    source_gcs_bucket: str = ""
    """GCS bucket holding the observed agents' source snapshots, keyed by agent
    name and deployment revision. Set via `AQA_SOURCE_GCS_BUCKET`."""

    traces_gcs_bucket: str = ""
    """GCS bucket AQA writes its own OTLP spans to. Set via
    `AQA_TRACES_GCS_BUCKET`, defaulting to `jobs_gcs_bucket` to avoid engine
    recreation."""

    content_capture_mode: str = ""
    """OpenTelemetry GenAI content capture mode from
    `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` (resolves to
    `NO_CONTENT` when unset).

    Reported in config metadata so tools like `dump-traces` can determine
    whether exported spans contain conversation turns. AQuA does not evaluate
    this setting directly; it is consumed by OpenTelemetry instrumentation."""

    quality_analysis_mode: str = DEFAULT_QUALITY_ANALYSIS_MODE
    """Quality analysis strategy: ``session_review`` reviews each session with
    one LLM call, and ``eval_service`` scores it against eval metrics in Gemini platform.
    Set via `AQA_QUALITY_ANALYSIS_MODE`."""

    metrics_gcs_bucket: str = ""
    """GCS bucket holding the code-metric library. Set via
    `AQA_METRICS_GCS_BUCKET`.

    Each published metric joins the session-review sweep. An unset bucket, or
    one holding no metric, leaves that sweep running on its own.

    Bucket contents execute in-process with the agent's credentials.
    Write access is equivalent to deploying AQuA."""

    telemetry_ingestion_source: str = "big_query"
    """Where to pull telemetry from (``big_query`` or ``cloud_ops``). Set
    via `TELEMETRY_INGESTION_SOURCE`."""

    observed_project_id: str = ""
    """Project the observed agent runs in: on Agent Runtime, Cloud Run or GKE.
    It owns `telemetry_dataset` and, for ``cloud_ops``, the logs message
    content is read from, and `aqua attach --apply` puts the audit sink on the
    agent's updates there. Empty means `project_id`. Set by `aqua attach` only;
    read it through `resolve_observed_project_id`.

    Ingestion jobs still run in `project_id`, on AQuA's own quota and
    credentials: BigQuery reads a table in another project, but only in the
    job's location, which is `telemetry_location`."""

    telemetry_dataset: str = DEFAULT_TELEMETRY_DATASETS["big_query"]
    """BigQuery dataset holding the telemetry `telemetry_ingestion_source`
    reads. Set via `AQA_TELEMETRY_DATASET`; defaults per source
    from `DEFAULT_TELEMETRY_DATASETS`."""

    telemetry_table: str = DEFAULT_TELEMETRY_TABLES["big_query"]
    """BigQuery table or view within `telemetry_dataset`. Set via
    `AQA_TELEMETRY_TABLE`; defaults per source from
    `DEFAULT_TELEMETRY_TABLES`."""

    telemetry_location: str = DEFAULT_BIGQUERY_LOCATION
    """BigQuery location of `telemetry_dataset`, and the location ingestion
    jobs run in (e.g. ``"us-central1"``). Resolved by `_resolve_bigquery_location`."""

    data_evaluation_cap: int = 1000
    """Maximum number of records to pull into a single evaluation run.
    Set via `DATA_EVALUATION_CAP`."""

    multi_turn_metrics: list[str] = field(
        default_factory=lambda: [
            "task_success",
            "tool_use_quality",
            "trajectory_quality",
        ]
    )
    """Metrics evaluated over a multi-turn conversation. Set via the
    comma-separated `MULTI_TURN_METRICS` env var."""

    single_turn_metrics: list[str] = field(default_factory=list)
    """Metrics evaluated over a single turn. Set via the
    comma-separated `SINGLE_TURN_METRICS` env var (empty by default)."""

    data_lookback_window: int = 7
    """How many days of telemetry to consider. Set via
    `DATA_LOOKBACK_WINDOW`."""

    sync_investigation: bool = False
    """Whether to run the investigation synchronously inline (for local dev).
    Set via `AQA_SYNC_INVESTIGATION`, and implied by `standalone`."""

    standalone: bool = False
    """Whether AQA runs on its own: no dataset, no engine, no scheduler. Set via
    `AQA_STANDALONE`.

    Storage becomes one in-process `standalone.InMemoryStore` in place of the
    dataset, and files under `.aqua/job/` in place of the jobs bucket, which
    `backends.configure_providers` binds to every provider. Telemetry and the
    model calls are unchanged: the observed agent is deployed and exporting
    either way, and clustering, merging and verification still reach Gemini platform.
    The constraint is no stored state in a Cloud project, not no cloud.

    Named `standalone` rather than `local`, because "local" already means the
    dashboard on your laptop against a deployed engine, which needs a finished
    `terraform apply`.
    """

    pending_run_lease_minutes: int = 0
    """How long a run may sit `pending` before the next submission fails it.
    Off by default; 30 is the value to set, matching the dashboard's
    `STALE_AFTER_MS` and leaving a job still queued alone. Set via
    `AQA_PENDING_RUN_LEASE_MINUTES`."""

    agent_revision_trigger_delay_seconds: int = (
        DEFAULT_AGENT_REVISION_TRIGGER_DELAY_SECONDS
    )
    """Seconds to wait after an observed-agent update before investigating
    (0 = investigate immediately). The delay lets traces for the new revision
    accumulate before the investigation runs. Set via
    `AQA_AGENT_REVISION_TRIGGER_DELAY_SECONDS`, or by `aqua attach`."""

    delay_task_queue: str = ""
    """Full Cloud Tasks queue path
    (`projects/{p}/locations/{loc}/queues/{q}`) the update path enqueues the
    delayed trigger task into. Set via `AQA_DELAY_TASK_QUEUE`."""

    ambient_caller_sa: str = ""
    """Service-account email whose OAuth token the delayed Cloud Task presents
    when it POSTs to the engine's trigger route. Set via
    `AQA_AMBIENT_CALLER_SA`."""

    ambient_topic: str = ""
    """Pub/Sub topic (`projects/{p}/topics/{t}`) an attached agent's update
    sink publishes to. Reported to `aqua attach --apply`, which wires the
    agent's triggers from it; empty in a deployment that runs its observed
    agent's triggers itself. Set via `AQA_AMBIENT_TOPIC`."""

    def _apply_standalone_overrides(self) -> None:
        """Make the rest of the configuration agree with `standalone`.

        Corrections, not defaults. A stray environment variable must not leave a
        standalone run half-deployed:

        - **The sweep runs inline.** There is no Agent Runtime to submit to.
        - **The delayed update path is off.** It needs a queue, a caller service
          account and an engine id. Without all three it logs a warning and
          no-ops on every run.
        - **AQuA's own buckets are off.** Standalone writes nothing to a Cloud
          project. Without them, chat transcripts stay in memory and AQuA's
          spans are not exported. The goal, the memories and the attached
          agent's configuration are files under `.aqua/job/` instead.

        Anything overridden is logged by name. Nothing here raises: a standalone
        run is what somebody asked for, and it should start.
        """
        if not self.sync_investigation:
            object.__setattr__(self, "sync_investigation", True)
        ignored = {
            "delay_task_queue": "there is no engine for the delayed update path",
            "ambient_caller_sa": "there is no engine for the delayed update path",
            "jobs_gcs_bucket": "standalone writes nothing to a Cloud project",
            "traces_gcs_bucket": "standalone writes nothing to a Cloud project",
        }
        for name, reason in ignored.items():
            if getattr(self, name):
                _log_ignored_setting(name, reason)
                object.__setattr__(self, name, "")

    def __post_init__(self) -> None:
        """Validate and normalize fields.

        Invalid enum-like values and broken ambient wiring raise; out-of-range
        non-ambient numbers are clamped and unknown metrics dropped (logged). The
        dataclass is frozen, so normalized values are written back via
        `object.__setattr__`.
        """
        # Coerce plain dicts back into `Model` for asdict()/JSON round-trips
        # (`effective_config.load` and the durable-run boundary rebuild Config
        # via `Config(**dataclasses.asdict(config))`, where nested Models have
        # been flattened to dicts).
        for model_field in (
            "base_model",
            "insights_model",
            "verification_model",
        ):
            value = getattr(self, model_field)
            if isinstance(value, dict):
                object.__setattr__(self, model_field, Model(**value))

        if (
            self.telemetry_ingestion_source
            not in ALLOWED_TELEMETRY_INGESTION_SOURCES
        ):
            allowed = ", ".join(sorted(ALLOWED_TELEMETRY_INGESTION_SOURCES))
            raise ValueError(
                "telemetry_ingestion_source must be one of "
                f"{{{allowed}}}, got {self.telemetry_ingestion_source!r}."
            )

        if self.quality_analysis_mode not in ALLOWED_QUALITY_ANALYSIS_MODES:
            allowed = ", ".join(sorted(ALLOWED_QUALITY_ANALYSIS_MODES))
            raise ValueError(
                "quality_analysis_mode must be one of "
                f"{{{allowed}}}, got {self.quality_analysis_mode!r}."
            )

        object.__setattr__(
            self,
            "data_evaluation_cap",
            _clamp(
                "data_evaluation_cap",
                self.data_evaluation_cap,
                MAX_DATA_EVALUATION_CAP,
            ),
        )
        object.__setattr__(
            self,
            "data_lookback_window",
            _clamp(
                "data_lookback_window",
                self.data_lookback_window,
                MAX_DATA_LOOKBACK_WINDOW,
            ),
        )
        _require_non_negative(
            "agent_revision_trigger_delay_seconds",
            self.agent_revision_trigger_delay_seconds,
            "AQA_AGENT_REVISION_TRIGGER_DELAY_SECONDS",
        )
        # Standalone implies the rest of its own mode, here rather than in
        # `load()`, so a Config rebuilt from a dict -- which is how the durable
        # boundary and `effective_config.load` reconstruct one -- cannot come
        # back as standalone with the wiring of a deployment.
        if self.standalone:
            self._apply_standalone_overrides()
        # The delayed update path needs BOTH the Cloud Tasks queue and the caller
        # SA; exactly one set is broken wiring that fails only later, when an
        # update fires.
        if bool(self.delay_task_queue) != bool(self.ambient_caller_sa):
            raise ValueError(
                "partial ambient update-path config: set both delay_task_queue "
                "and ambient_caller_sa, or neither"
            )
        object.__setattr__(
            self,
            "insights_auto_resolve_days",
            _clamp_min0(
                "insights_auto_resolve_days", self.insights_auto_resolve_days
            ),
        )
        object.__setattr__(
            self,
            "multi_turn_metrics",
            _filter_metrics(
                "multi_turn_metrics",
                self.multi_turn_metrics,
                MULTI_TURN_METRICS.keys(),
            ),
        )
        object.__setattr__(
            self,
            "single_turn_metrics",
            _filter_metrics(
                "single_turn_metrics",
                self.single_turn_metrics,
                SINGLE_TURN_METRICS.keys(),
            ),
        )

    def resolve_observed_project_id(self) -> str:
        """Resolves the project the observed agent and its telemetry are in.

        Returns:
            `observed_project_id`, or `project_id` when it is empty.
        """
        return self.observed_project_id or self.project_id

    def copy_with_agent(self, agent: ObservedAgentConfig) -> Config:
        """Returns a copy with observed-agent fields updated from `agent`.

        `dataclasses.replace` re-runs `__post_init__` to validate the combined
        values.

        Args:
            agent: Configuration containing the new observed-agent fields.

        Returns:
            New Config instance with updated observed-agent settings.
        """
        return dataclasses.replace(
            self,
            **{
                f.name: getattr(agent, f.name)
                for f in dataclasses.fields(agent)
            },
        )


@dataclass(frozen=True)
class ObservedAgentConfig:
    """The settings that change when AQuA is pointed at a different agent.

    A projection of `Config` rather than a member of it: `Config` is flattened
    with `dataclasses.asdict` at boundaries that outlive the process (a run's
    stored `effective_config`, the `/config` route), so nesting would change
    the shape of rows already written. Names, types and defaults mirror
    `Config`; `tests/test_observed_agent_config.py` pins that, and pins the two
    sides partitioning `Config` exactly.

    Validation stays in `Config.__post_init__`, so `copy_with_agent` is where a
    value is checked -- one gate rather than two that can disagree.

    `observed_deployment_name`, `default`, `investigation_schedule`,
    `scheduled_trigger_enabled` and `observed_engine_id` belong here too, and
    arrive with the GCS object that carries this config. They are not `Config`
    fields today, and a projection cannot hold what it has nothing to project.
    """

    observed_agent_name: str
    """Name of the watched agent."""

    telemetry_ingestion_source: str = "big_query"
    """Where its telemetry is pulled from."""

    observed_project_id: str = ""
    """Project it runs in, which owns its telemetry; empty means AQuA's own."""

    telemetry_dataset: str = DEFAULT_TELEMETRY_DATASETS["big_query"]
    """BigQuery dataset holding that telemetry."""

    telemetry_table: str = DEFAULT_TELEMETRY_TABLES["big_query"]
    """Table or view within `telemetry_dataset`."""

    telemetry_location: str = DEFAULT_BIGQUERY_LOCATION
    """BigQuery location of `telemetry_dataset`."""

    quality_analysis_mode: str = DEFAULT_QUALITY_ANALYSIS_MODE
    """How its sessions are scored."""

    multi_turn_metrics: list[str] = field(
        default_factory=lambda: [
            "task_success",
            "tool_use_quality",
            "trajectory_quality",
        ]
    )
    """Metrics evaluated over one of its conversations."""

    single_turn_metrics: list[str] = field(default_factory=list)
    """Metrics evaluated over one of its turns."""

    data_lookback_window: int = 7
    """How many days of its telemetry an investigation considers."""

    data_evaluation_cap: int = 1000
    """Most records one of its investigations pulls in."""

    insights_auto_resolve_days: int = 14
    """Triage policy: resolve one of its insights after this long unseen."""

    insights_verification_enabled: bool | None = None
    """Triage policy: whether its candidate issues are checked against their
    own traces."""

    insights_verification_enforced: bool | None = None
    """Triage policy: whether a negative verdict withholds one of its
    candidates."""

    agent_revision_trigger_delay_seconds: int = (
        DEFAULT_AGENT_REVISION_TRIGGER_DELAY_SECONDS
    )
    """How long to wait after it is updated before investigating."""


def extract_agent_config(config: Config) -> ObservedAgentConfig:
    """Projects observed-agent settings out of a full `Config`.

    Args:
        config: Full configuration to project.

    Returns:
        ObservedAgentConfig containing only observed-agent settings.
    """
    return ObservedAgentConfig(
        **{
            f.name: getattr(config, f.name)
            for f in dataclasses.fields(ObservedAgentConfig)
        }
    )


def is_verification_enabled(setting: bool | None) -> bool:
    """Determines whether the cluster verification pass should run.

    On unless the flag says otherwise, under every analysis mode. The pass is
    what writes an insight's description and the label a reader acts on, so a
    sweep that skips it produces insights nobody can triage. The flag stays for
    a deployment that needs the pass off while something else is investigated.

    Resolved on demand rather than into a field of `Config`, because the flag
    can be overridden after the config was read.

    Args:
        setting: Explicit boolean flag override, or None for default.

    Returns:
        True if verification should run, False otherwise.
    """
    return True if setting is None else setting


def is_verification_enforced(setting: bool | None) -> bool:
    """Determines whether a negative verdict withholds the candidate insight.

    Off unless the flag says otherwise. A trace that omits the tool error the
    defect turns on reads to the judge as no defect, so an enforced pass drops
    real issues; unenforced it still writes the label and description, and the
    verdict rides along for a triager. Turn it on where the traces carry full
    tool errors and a wrong insight costs more than a missed one.

    Resolved on demand rather than into a field of `Config`, because the flag
    can be overridden after the config was read.

    Args:
        setting: Explicit boolean flag override, or None for default.

    Returns:
        True if negative verdicts should withhold candidates, False otherwise.
    """
    return False if setting is None else setting


def _clamp(field_name: str, value: int, maximum: int) -> int:
    """Clamps `value` into the inclusive range [1, maximum].

    Out-of-range values are adjusted with a logged warning rather than
    raising, so a misconfigured deploy degrades gracefully.

    Args:
        field_name: Name of the configuration field (for warning logs).
        value: Integer value to clamp.
        maximum: Upper bound.

    Returns:
        Clamped integer value.
    """
    if value < 1:
        logger.warning(
            "%s=%d is below the minimum of 1; clamping to 1.", field_name, value
        )
        return 1
    if value > maximum:
        logger.warning(
            "%s=%d exceeds the maximum of %d; clamping to %d.",
            field_name,
            value,
            maximum,
            maximum,
        )
        return maximum
    return value


@functools.cache
def _log_ignored_setting(name: str, reason: str) -> None:
    """Logs, once per process, that standalone mode ignores a setting.

    A `Config` is rebuilt several times at start-up and once per run, and each
    rebuild applies the same correction. Logging it every time would bury it.

    Args:
        name: Name of the ignored configuration field.
        reason: Why standalone mode ignores the field.
    """
    logger.warning("standalone mode ignores %s; %s.", name, reason)


def _clamp_min0(field_name: str, value: int) -> int:
    """Clamps `value` to a minimum of 0.

    A negative value degrades to 0 with a warning. Separate from `_clamp`
    because 0 is a meaningful "disabled" value here.

    Args:
        field_name: Name of the configuration field (for warning logs).
        value: Integer value to clamp.

    Returns:
        Non-negative integer value.
    """
    if value < 0:
        logger.warning(
            "%s=%d is below the minimum of 0; clamping to 0.", field_name, value
        )
        return 0
    return value


def _require_non_negative(field_name: str, value: int, env_var: str) -> None:
    """Validates that `value` is non-negative.

    0 is an allowed sentinel representing immediate execution or disabled delay.

    Args:
        field_name: Name of the configuration field.
        value: Value to validate.
        env_var: Corresponding environment variable name.

    Raises:
        ValueError: If `value` is negative.
    """
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0 (set {env_var})")


def _filter_metrics(
    field_name: str, values: list[str], allowed: Collection[str]
) -> list[str]:
    """Filters metrics to those in `allowed`, preserving order and uniqueness.

    Unknown metrics are removed with a logged warning rather than
    raising.

    Args:
        field_name: Name of the metric field (for warning logs).
        values: List of metric names to filter.
        allowed: Collection of supported metric names.

    Returns:
        Filtered list of unique, allowed metric names.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for metric in values:
        if metric not in allowed:
            dropped.append(metric)
        elif metric not in kept:
            kept.append(metric)
    if dropped:
        logger.warning(
            "%s contained unsupported metrics %s; dropping them. Allowed: %s.",
            field_name,
            dropped,
            sorted(allowed),
        )
    return kept


def _read_required_env(env_var: str) -> str:
    """Reads a required environment variable.

    Args:
        env_var: Environment variable name.

    Returns:
        The string value of the environment variable.

    Raises:
        RuntimeError: If the environment variable is not set or empty.
    """
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(f"{env_var} is not set.")
    return value


def _parse_csv(env_var: str, default: list[str]) -> list[str]:
    """Parses a comma-separated environment variable into a list of strings.

    Returns `default` when the var is unset. An explicitly empty value
    yields an empty list.

    Args:
        env_var: Environment variable name.
        default: Fallback list when the variable is unset.

    Returns:
        List of trimmed strings, or `default` when unset.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_optional_bool(env_var: str) -> bool | None:
    """Parses a boolean environment variable, returning None when unset or blank.

    A field whose default is derived rather than fixed has to tell an explicit
    ``false`` from an absent value. Anything but ``true``/``1`` is false.

    Args:
        env_var: Environment variable name.

    Returns:
        Boolean value if set, or None if unset/empty.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    return raw.lower() in ("true", "1")


def _parse_int(env_var: str, default: int) -> int:
    """Parses an integer environment variable.

    Args:
        env_var: Environment variable name.
        default: Fallback integer value when unset.

    Returns:
        Parsed integer value or `default`.

    Raises:
        RuntimeError: If the value cannot be parsed as an integer.
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{env_var} must be an integer, got {raw!r}."
        ) from exc


def _resolve_bigquery_location(env_var: str) -> str:
    """Resolves a BigQuery location for ingestion jobs.

    The telemetry the agent reads and the tables it owns can live in
    different regions, so each has its own env var (`env_var`) but shares
    this precedence:
      1. An explicit `env_var` value (always wins; how local runs and
         overrides pin a region).
      2. The agent's region when deployed: ``GOOGLE_CLOUD_LOCATION`` (set
         for Gemini platform and Agent Runtime). The ``"global"``
         pseudo-location is ignored because BigQuery has no ``global``
         location.
      3. `DEFAULT_BIGQUERY_LOCATION` as a local fallback.

    Args:
        env_var: Environment variable specifying the explicit location override.

    Returns:
        Resolved BigQuery location string.
    """
    explicit = os.environ.get(env_var)
    if explicit and explicit.strip():
        return explicit.strip()

    agent_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
    if agent_location and agent_location.lower() != "global":
        return agent_location

    return DEFAULT_BIGQUERY_LOCATION


def _resolve_engine_location() -> str:
    """Resolves the region the agent's own engine lives in.

    `GOOGLE_CLOUD_LOCATION` says where models are served and is legitimately
    `global`, which agents-cli sets. An engine is regional and its query-job API
    answers 501 outside a region, so `AQA_ENGINE_LOCATION` overrides it and
    `DEFAULT_ENGINE_LOCATION` is the last resort.

    Returns:
        Resolved engine region string.
    """
    explicit = os.environ.get("AQA_ENGINE_LOCATION", "").strip()
    if explicit:
        return explicit

    agent_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
    if agent_location and agent_location.lower() != "global":
        return agent_location

    return DEFAULT_ENGINE_LOCATION


def _resolve_model(env_prefix: str, default_model: str) -> Model:
    """Builds a `Model` from ``{env_prefix}_MODEL`` / ``{env_prefix}_LOCATION``.

    Model falls back to ``default_model``; location falls back to
    `DEFAULT_LLM_LOCATION` (`global`), independent of `GOOGLE_CLOUD_LOCATION`.

    Args:
        env_prefix: Prefix for environment variables (e.g. "AQA_BASE").
        default_model: Fallback model name when unset.

    Returns:
        Configured Model instance.
    """
    return Model(
        model=os.environ.get(f"{env_prefix}_MODEL") or default_model,
        location=os.environ.get(f"{env_prefix}_LOCATION")
        or DEFAULT_LLM_LOCATION,
    )


def load() -> Config:
    """Reads all environment variables and returns a resolved `Config` instance.

    Returns:
        Populated Config object.
    """
    jobs_gcs_bucket = os.environ.get("AQA_JOBS_GCS_BUCKET", "")
    telemetry_source = os.environ.get("TELEMETRY_INGESTION_SOURCE", "big_query")
    return Config(
        observed_agent_name=os.environ.get("AQA_OBSERVED_AGENT_NAME", ""),
        project_id=_read_required_env("GOOGLE_CLOUD_PROJECT"),
        location=_resolve_engine_location(),
        base_model=_resolve_model("AQA_BASE", DEFAULT_BASE_MODEL),
        insights_model=_resolve_model("AQA_INSIGHTS", DEFAULT_INSIGHTS_MODEL),
        verification_model=_resolve_model(
            "AQA_VERIFICATION", DEFAULT_VERIFICATION_MODEL
        ),
        insights_match_model=os.environ.get(
            "AQA_INSIGHTS_MATCH_MODEL", DEFAULT_INSIGHTS_MATCH_MODEL
        ),
        selector_ai_model=os.environ.get(
            "AQA_SELECTOR_AI_MODEL", DEFAULT_SELECTOR_AI_MODEL
        ),
        aqa_dataset=os.environ.get("AQA_DATASET", "aqua_insights"),
        insights_auto_resolve_days=_parse_int(
            "AQA_INSIGHTS_AUTO_RESOLVE_DAYS", 14
        ),
        insights_verification_enabled=_parse_optional_bool(
            "AQA_INSIGHTS_VERIFICATION_ENABLED"
        ),
        insights_verification_enforced=_parse_optional_bool(
            "AQA_INSIGHTS_VERIFICATION_ENFORCED"
        ),
        aqa_engine_id=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", ""),
        jobs_gcs_bucket=jobs_gcs_bucket,
        source_gcs_bucket=os.environ.get("AQA_SOURCE_GCS_BUCKET", ""),
        traces_gcs_bucket=os.environ.get("AQA_TRACES_GCS_BUCKET")
        or jobs_gcs_bucket,
        content_capture_mode=os.environ.get(
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", ""
        ),
        quality_analysis_mode=os.environ.get(
            "AQA_QUALITY_ANALYSIS_MODE", DEFAULT_QUALITY_ANALYSIS_MODE
        ),
        metrics_gcs_bucket=os.environ.get("AQA_METRICS_GCS_BUCKET", ""),
        telemetry_ingestion_source=telemetry_source,
        telemetry_dataset=os.environ.get("AQA_TELEMETRY_DATASET")
        or DEFAULT_TELEMETRY_DATASETS.get(telemetry_source, ""),
        telemetry_table=os.environ.get("AQA_TELEMETRY_TABLE")
        or DEFAULT_TELEMETRY_TABLES.get(telemetry_source, ""),
        telemetry_location=_resolve_bigquery_location("AQA_TELEMETRY_LOCATION"),
        aqa_dataset_location=_resolve_bigquery_location("AQA_DATASET_LOCATION"),
        data_evaluation_cap=_parse_int("DATA_EVALUATION_CAP", 1000),
        multi_turn_metrics=_parse_csv(
            "MULTI_TURN_METRICS",
            ["task_success", "tool_use_quality", "trajectory_quality"],
        ),
        single_turn_metrics=_parse_csv("SINGLE_TURN_METRICS", []),
        data_lookback_window=_parse_int("DATA_LOOKBACK_WINDOW", 7),
        sync_investigation=os.environ.get("AQA_SYNC_INVESTIGATION", "").lower()
        in ("true", "1"),
        standalone=os.environ.get("AQA_STANDALONE", "").lower()
        in ("true", "1"),
        pending_run_lease_minutes=_parse_int(
            "AQA_PENDING_RUN_LEASE_MINUTES", 0
        ),
        agent_revision_trigger_delay_seconds=_parse_int(
            "AQA_AGENT_REVISION_TRIGGER_DELAY_SECONDS",
            DEFAULT_AGENT_REVISION_TRIGGER_DELAY_SECONDS,
        ),
        delay_task_queue=os.environ.get("AQA_DELAY_TASK_QUEUE", ""),
        ambient_caller_sa=os.environ.get("AQA_AMBIENT_CALLER_SA", ""),
        ambient_topic=os.environ.get("AQA_AMBIENT_TOPIC", ""),
    )


# Loaded once at module import. Re-import the module (or call `load()`)
# in tests that need to override env vars.
config: Config = load()
