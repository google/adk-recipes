"""Utility functions for parsing Model Armor filter responses."""

import logging

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

logger = logging.getLogger(__name__)


def parse_model_armor_response(
    response: SanitizeModelResponseResponse | SanitizeUserPromptResponse,
) -> list[str] | None:
    """Analyzes the Model Armor response and returns a list of detected filter names.

    Returns:
        List of filter names (e.g. ["CSAM", "Prompt Injection and Jailbreaking"]),
        or None if no violations detected.
    """
    sanitization_result = response.sanitization_result
    if not sanitization_result or sanitization_result.filter_match_state == FilterMatchState.NO_MATCH_FOUND:
        return None

    detected_filters: list[str] = []
    filter_matches = sanitization_result.filter_results

    if "csam" in filter_matches:
        detected_filters.extend(_parse_csam_filter(filter_matches["csam"].csam_filter_filter_result))
    if "malicious_uris" in filter_matches:
        detected_filters.extend(
            _parse_malicious_uris_filter(filter_matches["malicious_uris"].malicious_uri_filter_result)
        )
    if "rai" in filter_matches:
        detected_filters.extend(_parse_rai_filter(filter_matches["rai"].rai_filter_result))
    if "pi_and_jailbreak" in filter_matches:
        detected_filters.extend(
            _parse_pi_and_jailbreak_filter(filter_matches["pi_and_jailbreak"].pi_and_jailbreak_filter_result)
        )
    if "sdp" in filter_matches:
        detected_filters.extend(_parse_sdp_filter(filter_matches["sdp"].sdp_filter_result))

    if detected_filters:
        logger.info("Model Armor detected filters: %s", detected_filters)
    return detected_filters if detected_filters else None


def _parse_csam_filter(csam_result: CsamFilterResult) -> list[str]:
    if csam_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["CSAM"]
    return []


def _parse_malicious_uris_filter(uri_result: MaliciousUriFilterResult) -> list[str]:
    if uri_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["Malicious URIs"]
    return []


def _parse_rai_filter(rai_result: RaiFilterResult) -> list[str]:
    if rai_result.match_state == FilterMatchState.MATCH_FOUND:
        return [
            filter_name
            for filter_name, matched in rai_result.rai_filter_type_results.items()
            if matched.match_state == FilterMatchState.MATCH_FOUND
        ]
    return []


def _parse_pi_and_jailbreak_filter(pi_result: PiAndJailbreakFilterResult) -> list[str]:
    if pi_result.match_state == FilterMatchState.MATCH_FOUND:
        return ["Prompt Injection and Jailbreaking"]
    return []


def _parse_sdp_filter(sdp_result: SdpFilterResult) -> list[str]:
    detected: list[str] = []

    inspect_result = sdp_result.inspect_result
    if inspect_result and inspect_result.match_state == FilterMatchState.MATCH_FOUND:
        for finding in inspect_result.findings:
            detected.append(finding.info_type.replace("_", " ").capitalize())

    deidentify_result = sdp_result.deidentify_result
    if deidentify_result and deidentify_result.match_state == FilterMatchState.MATCH_FOUND:
        for info_type in deidentify_result.info_types:
            detected.append(info_type.replace("_", " ").capitalize())

    return detected
