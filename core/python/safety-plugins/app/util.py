# Copyright 2025 Google LLC
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

"""Utility functions for Guardian."""

import logging
from typing import Any

from google.adk import runners
from google.cloud.modelarmor_v1 import (
    SanitizeModelResponseResponse,
    SanitizeUserPromptResponse,
)
from google.cloud.modelarmor_v1.types import (
    CsamFilterResult,
    FilterMatchState,
    MaliciousUriFilterResult,
    PiAndJailbreakFilterResult,
    RaiFilterResult,
    SdpFilterResult,
)
from google.genai import types

Runner = runners.Runner


async def run_prompt(
    user_id: str,
    app_name: str,
    runner: Runner,
    message: types.Content,
    session_id: str | None = None,
) -> tuple[str, str]:
    """Runs a prompt using the provided runner and returns the response.

    Args:
        user_id: The ID of the user.
        app_name: The name of the application.
        runner: The runner to use for running the prompt.
        message: The content of the message to send.
        session_id: The ID of an existing session.

    Returns:
        The response text from the agent.
    """
    try:
        if session_id is not None:
            session = await runner.session_service.get_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
        else:
            session = await runner.session_service.create_session(
                user_id=user_id,
                app_name=app_name,
            )
        if not session:
            raise ValueError("Session is None")

        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=message
        ):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                return (
                    event.author,
                    (event.content.parts[0].text or ""),
                )

    except Exception as e:
        return "SYSTEM", str(e)

    return f"{runner.agent.name}", "No response from the agent."


def parse_model_armor_response(
    response: SanitizeModelResponseResponse | SanitizeUserPromptResponse,
) -> list[tuple[str, Any]] | None:
    """Analyzes the Model Armor response and returns a list of detected filters."""
    sanitization_result = response.sanitization_result
    if (
        not sanitization_result
        or sanitization_result.filter_match_state
        == FilterMatchState.NO_MATCH_FOUND
    ):
        return None

    detected_filters = []
    filter_matches = sanitization_result.filter_results

    # Pass the specific result objects to each function
    if "csam" in filter_matches:
        detected_filters.extend(
            parse_csam_filter(filter_matches["csam"].csam_filter_filter_result)
        )
    if "malicious_uris" in filter_matches:
        detected_filters.extend(
            parse_malicious_uris_filter(
                filter_matches["malicious_uris"].malicious_uri_filter_result
            )
        )
    if "rai" in filter_matches:
        detected_filters.extend(
            parse_rai_filter(filter_matches["rai"].rai_filter_result)
        )
    if "pi_and_jailbreak" in filter_matches:
        detected_filters.extend(
            parse_pi_and_jailbreak_filter(
                filter_matches[
                    "pi_and_jailbreak"
                ].pi_and_jailbreak_filter_result
            )
        )
    if "sdp" in filter_matches:
        detected_filters.extend(
            parse_sdp_filter(filter_matches["sdp"].sdp_filter_result)
        )
    logging.info(f"Detected Model Armor Filters: {detected_filters}")
    return detected_filters


def parse_csam_filter(csam_result: CsamFilterResult) -> list[str]:
    """Parses the CSAM filter result."""
    if csam_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["CSAM"]
    return []


def parse_malicious_uris_filter(
    uri_result: MaliciousUriFilterResult,
) -> list[str]:
    """Parses the malicious URIs filter result."""
    if uri_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["Malicious URIs"]
    return []


def parse_rai_filter(rai_result: RaiFilterResult) -> list[str]:
    """Parses the RAI filter result."""
    if rai_result.match_state == FilterMatchState.MATCH_FOUND:
        return [
            filter_name
            for filter_name, matched in rai_result.rai_filter_type_results.items()
            if matched.match_state == FilterMatchState.MATCH_FOUND
        ]
    return []


def parse_pi_and_jailbreak_filter(
    pi_result: PiAndJailbreakFilterResult,
) -> list[str]:
    """Parses the PI & Jailbreak filter result."""
    if pi_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["Prompt Injection and Jailbreaking"]
    return []


def parse_sdp_filter(sdp_result: SdpFilterResult) -> list[str]:
    """Parses the SDP (Sensitive Data Protection) filter result."""
    detected_filters = []

    inspect_result = sdp_result.inspect_result
    if (
        inspect_result
        and inspect_result.match_state == FilterMatchState.MATCH_FOUND
    ):
        for finding in inspect_result.findings:
            info_type = finding.info_type.replace("_", " ").capitalize()
            detected_filters.append(info_type)

    deidentify_result = sdp_result.deidentify_result
    if (
        deidentify_result
        and deidentify_result.match_state == FilterMatchState.MATCH_FOUND
    ):
        for info_type in deidentify_result.info_types:
            formatted_info_type = info_type.replace("_", " ").capitalize()
            detected_filters.append(formatted_info_type)

    return detected_filters
