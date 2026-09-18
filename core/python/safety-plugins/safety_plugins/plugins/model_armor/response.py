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

"""Read and validate Google's Model Armor protobuf responses.

This module is the only one read by BOTH the decision layer (:mod:`policy`)
and the observability layer (:mod:`telemetry`); folding it into either would
force the other to import it for the wrong reason.
"""

from typing import Any

from google.cloud import modelarmor_v1

from .constants import (
    SDP_FILTER,
    ModelArmorResponse,
)


class ModelArmorInvocationError(RuntimeError):
    """Model Armor returned an invocation result other than SUCCESS."""

    def __init__(self, *, invocation_result: str) -> None:
        self.invocation_result = invocation_result
        super().__init__(f"Model Armor invocation failed: {invocation_result}")


class InvalidModelArmorResponseError(RuntimeError):
    """Model Armor returned an internally inconsistent response."""

    def __init__(self) -> None:
        super().__init__("Model Armor returned an inconsistent filter verdict")


def filter_verdict(filter_result) -> tuple[str | None, Any | None]:
    """Unwrap one filter result to its (kind, verdict) pair.

    SDP nests a second oneof (inspect vs de-identify) inside its own result,
    so it is unwrapped twice.
    """
    result_kind = filter_result._pb.WhichOneof("filter_result")
    if result_kind is None:
        return None, None

    verdict = getattr(filter_result, result_kind)
    if result_kind != "sdp_filter_result":
        return result_kind, verdict

    result_kind = verdict._pb.WhichOneof("result")
    return (
        (result_kind, getattr(verdict, result_kind))
        if result_kind is not None
        else (None, None)
    )


def matched_filters(response: ModelArmorResponse) -> tuple[str, ...]:
    """Names of the filters that actually reported a match."""
    result = response.sanitization_result
    matched: list[str] = []
    for name, filter_result in result.filter_results.items():
        _, verdict = filter_verdict(filter_result)
        if (
            verdict is not None
            and getattr(verdict, "match_state", None)
            == modelarmor_v1.FilterMatchState.MATCH_FOUND
        ):
            matched.append(name)
    return tuple(sorted(matched))


def deidentified_text(response: ModelArmorResponse) -> str | None:
    """Google's de-identified replacement text, when it produced a usable one."""
    sdp_filter = response.sanitization_result.filter_results.get(SDP_FILTER)
    if sdp_filter is None:
        return None

    result_kind, verdict = filter_verdict(sdp_filter)
    if (
        result_kind != "deidentify_result"
        or verdict.execution_state
        != modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
        or verdict.match_state != modelarmor_v1.FilterMatchState.MATCH_FOUND
        or verdict.data._pb.WhichOneof("data_item") != "text"
    ):
        return None
    return verdict.data.text


def validate_response(response: ModelArmorResponse) -> None:
    """Reject anything but a complete, self-consistent scan.

    Only ``SUCCESS`` may be trusted: ``PARTIAL`` and ``FAILURE`` mean some
    filter did not run, so the content was never fully screened. Each returned
    filter must also report successful execution and an explicit verdict.
    """
    result = response.sanitization_result
    if result.invocation_result != modelarmor_v1.InvocationResult.SUCCESS:
        raise ModelArmorInvocationError(
            invocation_result=modelarmor_v1.InvocationResult(
                result.invocation_result
            ).name
        )
    valid_match_states = {
        modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
        modelarmor_v1.FilterMatchState.MATCH_FOUND,
    }
    if result.filter_match_state not in valid_match_states:
        raise InvalidModelArmorResponseError
    for filter_result in result.filter_results.values():
        _, verdict = filter_verdict(filter_result)
        if (
            verdict is None
            or verdict.execution_state
            != modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
            or verdict.match_state not in valid_match_states
        ):
            raise InvalidModelArmorResponseError
    if (
        result.filter_match_state == modelarmor_v1.FilterMatchState.MATCH_FOUND
    ) != bool(matched_filters(response)):
        raise InvalidModelArmorResponseError
