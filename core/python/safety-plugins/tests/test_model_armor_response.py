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

"""Validate individual filter verdicts, not only the aggregate result."""

import pytest
from google.cloud import modelarmor_v1

from safety_plugins.plugins.model_armor.response import (
    InvalidModelArmorResponseError,
    validate_response,
)


@pytest.fixture(
    params=[
        "rai_filter_result",
        "pi_and_jailbreak_filter_result",
        "malicious_uri_filter_result",
        "csam_filter_filter_result",
        "virus_scan_filter_result",
        "inspect_result",
        "deidentify_result",
    ]
)
def filter_response(request):
    verdict = {
        "execution_state": "EXECUTION_SUCCESS",
        "match_state": "NO_MATCH_FOUND",
    }
    field = request.param
    result = {field: verdict}
    name = "test_filter"
    if field in {"inspect_result", "deidentify_result"}:
        result = {"sdp_filter_result": result}
        name = "sdp"
    response = modelarmor_v1.SanitizeUserPromptResponse(
        sanitization_result={
            "invocation_result": "SUCCESS",
            "filter_match_state": "NO_MATCH_FOUND",
            "filter_results": {name: result},
        }
    )
    filter_result = response.sanitization_result.filter_results[name]
    if name == "sdp":
        filter_result = filter_result.sdp_filter_result
    return response, getattr(filter_result, field)


@pytest.mark.parametrize("match_state", ["NO_MATCH_FOUND", "MATCH_FOUND"])
def test_complete_filter_verdict_is_accepted(filter_response, match_state):
    response, verdict = filter_response
    verdict.match_state = match_state
    response.sanitization_result.filter_match_state = match_state

    validate_response(response)


@pytest.mark.parametrize(
    "field, value",
    [
        ("execution_state", "EXECUTION_SKIPPED"),
        ("execution_state", "FILTER_EXECUTION_STATE_UNSPECIFIED"),
        ("match_state", "FILTER_MATCH_STATE_UNSPECIFIED"),
    ],
)
def test_incomplete_filter_cannot_be_hidden_by_success(
    filter_response, field, value
):
    response, verdict = filter_response
    setattr(verdict, field, value)

    with pytest.raises(InvalidModelArmorResponseError):
        validate_response(response)


@pytest.mark.parametrize(
    "filter_result",
    [{}, {"sdp_filter_result": {}}],
    ids=["missing-filter-verdict", "missing-sdp-verdict"],
)
def test_missing_filter_verdict_cannot_be_hidden_by_success(filter_result):
    response = modelarmor_v1.SanitizeUserPromptResponse(
        sanitization_result={
            "invocation_result": "SUCCESS",
            "filter_match_state": "NO_MATCH_FOUND",
            "filter_results": {"sdp": filter_result},
        }
    )

    with pytest.raises(InvalidModelArmorResponseError):
        validate_response(response)
