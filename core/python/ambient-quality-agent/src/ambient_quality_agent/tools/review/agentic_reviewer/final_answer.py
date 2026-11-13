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

"""Extracts and validates findings from the reviewer's final text reply.

Because the final reply is unconstrained text rather than a schema-enforced
response, models may wrap the findings JSON in markdown code fences or prose.
`extract_raw_findings` tries the `FINAL_PARSE_*` strategies from strictest to
most lenient so those replies are not lost, and `build_findings` converts the
extracted dicts into validated `Finding` records.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Final

from ambient_quality_agent.tools.insights import findings as findings_module

if TYPE_CHECKING:
    from ambient_quality_agent.tools.insights.findings import Finding
    from google.genai import types

logger = logging.getLogger(__name__)

# Matches a final text wrapped in a single markdown code fence with an optional language tag.
_CODE_FENCE = re.compile(r"\A\s*```[\w-]*[ \t]*\n(.*?)\s*```\s*\Z", re.DOTALL)

# Matches an individual markdown code fence anywhere in a text.
_FENCED_BLOCK = re.compile(r"```[\w-]*[ \t]*\n(.*?)```", re.DOTALL)

FINAL_PARSE_STRICT: Final = "strict"
"""Parse path when the entire final text (or its single outer fence) is the findings object."""

FINAL_PARSE_FENCED: Final = "fenced"
"""Parse path when a fenced block inside the final text is the findings object."""

FINAL_PARSE_ARRAY: Final = "array"
"""Parse path when the final text (or its last fenced block) is a bare findings array."""

FINAL_PARSE_EMBEDDED: Final = "embedded"
"""Parse path when a ``{...}`` substring of the final text is the findings object."""

_NOT_JSON: Final = object()
"""Sentinel for text that fails JSON parsing."""


def parse_final_text(
    text: str, trajectory_id: str, known_agent_ids: set[str]
) -> tuple[list[Finding], str] | None:
    """Parse a final text answer into findings.

    Args:
        text: The reply's text.
        trajectory_id: Trajectory identifier for finding attribution.
        known_agent_ids: Valid agent identifiers; others are cleared.

    Returns:
        The findings, capped at `findings.MAX_FINDINGS_PER_TRAJECTORY`, and the
        parse path; None if the text holds no findings JSON.
    """
    extracted = extract_raw_findings(text)
    if extracted is None:
        return None
    raw_findings, parse_path = extracted
    findings = build_findings(raw_findings, trajectory_id, known_agent_ids)
    return findings, parse_path


def extract_reply_text(content: types.Content | None) -> str:
    """Concatenate non-thought text parts from a model reply.

    Args:
        content: Model reply content, or None if the reply is empty.

    Returns:
        Concatenated non-thought text parts, or an empty string if none exist.
    """
    return "".join(
        part.text
        for part in (content.parts or [] if content else [])
        if part.text and not part.thought
    )


def extract_raw_findings(text: str) -> tuple[list[Any], str] | None:
    """Extract the findings list from a final text reply.

    Tries extraction strategies in order from strictest to most lenient to
    handle replies wrapped in prose or markdown code fences:

    1. `FINAL_PARSE_STRICT`: the entire text (or a single outer code fence) is
       a JSON object with a ``findings`` list.
    2. `FINAL_PARSE_FENCED`: the last fenced block containing such an object.
    3. `FINAL_PARSE_ARRAY`: the entire text (or its last fenced block) is a
       JSON array, treated as the findings list.
    4. `FINAL_PARSE_EMBEDDED`: the last balanced ``{...}`` substring that
       decodes to such an object.

    Args:
        text: Final reply text.

    Returns:
        Tuple of ``(raw_findings, parse_path)`` for `build_findings`, or None
        if no strategy matches.
    """
    fenced = _CODE_FENCE.match(text)
    body = fenced.group(1) if fenced else text.strip()
    value = _parse_json_value(body)
    if _is_findings_object(value):
        return value["findings"], FINAL_PARSE_STRICT
    blocks = [match.group(1).strip() for match in _FENCED_BLOCK.finditer(text)]
    for block in reversed(blocks):
        value = _parse_json_value(block)
        if _is_findings_object(value):
            return value["findings"], FINAL_PARSE_FENCED
    for candidate in [body, *blocks[-1:]]:
        value = _parse_json_value(candidate)
        if isinstance(value, list):
            return value, FINAL_PARSE_ARRAY
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char == "{"]
    for start in reversed(starts):
        try:
            decoded, _ = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if _is_findings_object(decoded):
            return decoded["findings"], FINAL_PARSE_EMBEDDED
    return None


def build_findings(
    raw_findings: list[Any], trajectory_id: str, known_agent_ids: set[str]
) -> list[Finding]:
    """Validate raw findings and keep at most `findings.MAX_FINDINGS_PER_TRAJECTORY`.

    Reads the cap at call time so prompt variants can override it.

    Args:
        raw_findings: The findings list `extract_raw_findings` extracted.
        trajectory_id: Trajectory identifier for attribution.
        known_agent_ids: Valid agent identifiers for the session.

    Returns:
        The valid findings, in order, capped at
        `findings.MAX_FINDINGS_PER_TRAJECTORY`.
    """
    cap = findings_module.MAX_FINDINGS_PER_TRAJECTORY
    findings: list[Finding] = []
    for raw in raw_findings:
        finding = _build_finding(raw, trajectory_id, known_agent_ids)
        if finding is not None:
            findings.append(finding)
    if len(findings) > cap:
        logger.warning(
            "review: session %s reported %d findings; keeping the first %d.",
            trajectory_id or "<unknown>",
            len(findings),
            cap,
        )
    return findings[:cap]


def _build_finding(
    raw: Any, trajectory_id: str, known_agent_ids: set[str]
) -> Finding | None:
    """Validate and convert a raw finding dictionary to a `Finding` instance.

    Args:
        raw: Dictionary containing raw finding data.
        trajectory_id: Trajectory identifier for attribution.
        known_agent_ids: Valid agent identifiers for the session.

    Returns:
        A validated `Finding` instance with unknown agent IDs cleared, or None
        if the input is invalid or lacks behavior descriptions.
    """
    if not isinstance(raw, dict):
        logger.warning(
            "review: skipping a malformed finding: %r", type(raw).__name__
        )
        return None
    expected = str(raw.get("expected_behavior") or "").strip()
    actual = str(raw.get("actual_behavior") or "").strip()
    if not expected and not actual:
        return None
    agent_id = str(raw.get("agent_id") or "").strip()
    if agent_id and agent_id not in known_agent_ids:
        logger.warning(
            "review: session %s named agent %r, which it does not contain; "
            "leaving the finding unattributed.",
            trajectory_id or "<unknown>",
            agent_id,
        )
        agent_id = ""
    return findings_module.Finding(
        expected_behavior=expected,
        actual_behavior=actual,
        trajectory_id=trajectory_id,
        agent_id=agent_id,
    )


def _parse_json_value(text: str) -> Any:
    """Parse `text` as a single JSON value.

    Args:
        text: Candidate JSON string.

    Returns:
        Decoded JSON value, or `_NOT_JSON` if parsing fails.
    """
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _is_findings_object(value: Any) -> bool:
    """Check whether `value` is a dict containing a ``findings`` list.

    Args:
        value: Decoded JSON value.

    Returns:
        True if `value` is a dict whose ``findings`` entry is a list.
    """
    return isinstance(value, dict) and isinstance(value.get("findings"), list)
