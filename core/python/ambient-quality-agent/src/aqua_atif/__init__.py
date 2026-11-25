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

"""ATIF trajectory models and the rules AQuA applies to ATIF documents.

`aqua_atif.models` vendors Harbor v0.25.0's ATIF-v1.8 Pydantic models with five
small adjustments (see `aqua_atif.models`). Validation delegates required
fields, field types, and cross-field checks to those Harbor models, so a
document missing a required ATIF field fails validation (`DocumentRejectedError`
-> `not_ingested`), while a document missing optional fields passes and gets
`not_recorded` markers in `annotate_document`.

Imported by `ambient_quality_agent` and by converter plugins, which cannot
import `ambient_quality_*` code, so this package imports only the standard
library and Pydantic. None of its functions do file or network I/O; external
facts come in as arguments.

A file goes through these steps::

    parse_document            strict JSON; lone surrogates replaced
          │
    validate_document         Harbor v0.25.0 models + version rule + producer
          │                   extra.aqua rules; missing required fields or
          │                   invalid extra.aqua keys are rejected
    annotate_document         errors, markers ─► IngestStatus
          │
    build_reviewer_document   what the session reviewer reads
"""

from aqua_atif.annotate import (
    annotate_document,
    resolve_agent_revision,
)
from aqua_atif.errors import (
    AquaError,
    extract_error,
)
from aqua_atif.markers import (
    AGENT_REVISION_CHECK,
    AGENT_VERSION,
    COMPLETENESS_CHECK,
    COMPLETION_CHECK,
    CONTINUED_TRAJECTORY,
    DROPPED_CONTENT_CHECK,
    FAILED_CALL_CHECK,
    FINAL_METRICS,
    INSTRUCTION_CHECK,
    MEDIA_CHECK,
    MODEL_CHECK,
    REASONING_CHECK,
    STEP_MESSAGES,
    STEP_METRICS,
    STEP_MODEL_NAMES,
    STEP_REASONING,
    STEP_TIMESTAMPS,
    SUBAGENT_CHECK,
    SUBAGENT_TRAJECTORY,
    SYSTEM_PROMPT,
    TIMING_CHECK,
    TOKEN_CHECK,
    TOOL_CALL_RESULT,
    TOOL_DEFINITIONS,
    TOOL_ERROR_CHECK,
    TOOL_ERRORS,
    TOTALS_CHECK,
    UNLISTED_TOOL_CHECK,
    DisabledCheck,
    DisabledCheckUse,
    IngestStatus,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    SubagentIndex,
    add_embedded_subagent,
    add_markers,
    build_subagent_index,
    compute_ingest_status,
    find_embedded_subagent,
    has_subagent_id,
    list_disabled_checks,
    list_placed_markers,
    resolve_disabled_check,
    resolve_subagent,
)
from aqua_atif.models import (
    Agent,
    AudioMediaType,
    AudioSource,
    ContentPart,
    FinalMetrics,
    ImageMediaType,
    ImageSource,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)
from aqua_atif.parsing import (
    MAX_JSON_DEPTH,
    MAX_REASON_LENGTH,
    DocumentRejectedError,
    ParsedJson,
    bound_text,
    parse_document,
)
from aqua_atif.paths import (
    AQUA_KEY,
    RECORDS_TOOL_ERRORS,
    UNNAMED_FIELD,
    Holder,
    HolderKind,
    JsonPath,
    format_json_path,
    format_location,
    format_path,
    get_aqua,
    get_value,
    list_holders,
    parse_json_path,
    split_holder_path,
)
from aqua_atif.producer import (
    AquaErrorDict,
    FilePart,
    FileSource,
    ResultAqua,
    StepAqua,
    ToolCallAqua,
    TrajectoryAqua,
)
from aqua_atif.reviewer import (
    build_reviewer_document,
)
from aqua_atif.validation import (
    SchemaVersion,
    ValidatedDocument,
    validate_document,
)

__all__ = [
    "AGENT_REVISION_CHECK",
    "AGENT_VERSION",
    "AQUA_KEY",
    "COMPLETENESS_CHECK",
    "COMPLETION_CHECK",
    "CONTINUED_TRAJECTORY",
    "DROPPED_CONTENT_CHECK",
    "FAILED_CALL_CHECK",
    "FINAL_METRICS",
    "INSTRUCTION_CHECK",
    "MAX_JSON_DEPTH",
    "MAX_REASON_LENGTH",
    "MEDIA_CHECK",
    "MODEL_CHECK",
    "REASONING_CHECK",
    "RECORDS_TOOL_ERRORS",
    "STEP_MESSAGES",
    "STEP_METRICS",
    "STEP_MODEL_NAMES",
    "STEP_REASONING",
    "STEP_TIMESTAMPS",
    "SUBAGENT_CHECK",
    "SUBAGENT_TRAJECTORY",
    "SYSTEM_PROMPT",
    "TIMING_CHECK",
    "TOKEN_CHECK",
    "TOOL_CALL_RESULT",
    "TOOL_DEFINITIONS",
    "TOOL_ERRORS",
    "TOOL_ERROR_CHECK",
    "TOTALS_CHECK",
    "UNLISTED_TOOL_CHECK",
    "UNNAMED_FIELD",
    "Agent",
    "AquaError",
    "AquaErrorDict",
    "AudioMediaType",
    "AudioSource",
    "ContentPart",
    "DisabledCheck",
    "DisabledCheckUse",
    "DocumentRejectedError",
    "FilePart",
    "FileSource",
    "FinalMetrics",
    "Holder",
    "HolderKind",
    "ImageMediaType",
    "ImageSource",
    "IngestStatus",
    "JsonPath",
    "Metrics",
    "MissingMarker",
    "Observation",
    "ObservationResult",
    "ParsedJson",
    "PendingMissingMarker",
    "Reason",
    "ResultAqua",
    "SchemaVersion",
    "Step",
    "StepAqua",
    "SubagentIndex",
    "SubagentTrajectoryRef",
    "ToolCall",
    "ToolCallAqua",
    "Trajectory",
    "TrajectoryAqua",
    "ValidatedDocument",
    "add_embedded_subagent",
    "add_markers",
    "annotate_document",
    "bound_text",
    "build_reviewer_document",
    "build_subagent_index",
    "compute_ingest_status",
    "extract_error",
    "find_embedded_subagent",
    "format_json_path",
    "format_location",
    "format_path",
    "get_aqua",
    "get_value",
    "has_subagent_id",
    "list_disabled_checks",
    "list_holders",
    "list_placed_markers",
    "parse_document",
    "parse_json_path",
    "resolve_agent_revision",
    "resolve_disabled_check",
    "resolve_subagent",
    "split_holder_path",
    "validate_document",
]
