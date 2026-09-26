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

"""Build metadata only; never include prompts, replacements or error text."""

from typing import Any

from google.cloud import modelarmor_v1

from .constants import SDP_FILTER, ModelArmorAction, ModelArmorResponse
from .response import filter_verdict, matched_filters


def _invocation_name(value: int) -> str:
    try:
        return modelarmor_v1.InvocationResult(value).name
    except ValueError:
        return "UNKNOWN"


def screening_fields(
    response: ModelArmorResponse | None,
    *,
    action: ModelArmorAction,
    reasons: tuple[str, ...] = (),
    error: Exception | None = None,
) -> dict[str, Any]:
    """Build structured fields without logging a protobuf or exception."""
    fields: dict[str, Any] = {
        "action": action.value,
        "reasons": list(reasons),
        "invocation_result": "UNAVAILABLE",
        "matched_filters": [],
    }
    if response is not None:
        result = response.sanitization_result
        fields.update(
            invocation_result=_invocation_name(result.invocation_result),
            matched_filters=list(matched_filters(response)),
        )
        sdp = result.filter_results.get(SDP_FILTER)
        if sdp is not None:
            kind, verdict = filter_verdict(sdp)
            if kind == "deidentify_result":
                fields[SDP_FILTER] = {
                    "info_types": list(verdict.info_types),
                    "transformed_bytes": verdict.transformed_bytes,
                }
    if error is not None:
        fields["error_kind"] = type(error).__name__
    return fields
