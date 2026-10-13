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

"""Pure allow/redact/block decisions, independent of ADK and Google RPCs."""

from dataclasses import dataclass, field

from .constants import (
    MODEL_ARMOR_UNAVAILABLE_REASON,
    MODEL_RESPONSE_REMOVED_MESSAGE,
    SAFETY_SERVICE_UNAVAILABLE_MESSAGE,
    SDP_FILTER,
    UNSAFE_TOOL_OUTPUT_MESSAGE,
    USER_PROMPT_REMOVED_MESSAGE,
    ModelArmorAction,
    ModelArmorResponse,
    ScreeningStage,
)
from .response import deidentified_text, matched_filters


@dataclass(frozen=True, slots=True)
class Decision:
    """A decision never exposes transformed content through its repr."""

    action: ModelArmorAction
    reasons: tuple[str, ...] = ()
    redacted_text: str | None = field(default=None, repr=False)
    available: bool = True


def decide(response: ModelArmorResponse | None) -> Decision:
    """Apply the same strict policy to every successfully validated scan."""
    if response is None:
        return Decision(
            action=ModelArmorAction.BLOCK,
            reasons=(MODEL_ARMOR_UNAVAILABLE_REASON,),
            available=False,
        )
    matches = matched_filters(response)
    replacement = deidentified_text(response)
    reasons = tuple(
        name
        for name in matches
        if not (name == SDP_FILTER and replacement is not None)
    )
    if reasons:
        return Decision(action=ModelArmorAction.BLOCK, reasons=reasons)
    if replacement is not None:
        return Decision(
            action=ModelArmorAction.REDACT, redacted_text=replacement
        )
    return Decision(action=ModelArmorAction.ALLOW)


def block_message(stage: ScreeningStage, decision: Decision) -> str:
    """Render a replacement without including screened content."""
    if not decision.available:
        return SAFETY_SERVICE_UNAVAILABLE_MESSAGE
    if stage == ScreeningStage.USER_PROMPT:
        message = USER_PROMPT_REMOVED_MESSAGE
    elif stage == ScreeningStage.TOOL_OUTPUT:
        message = UNSAFE_TOOL_OUTPUT_MESSAGE
    else:
        message = MODEL_RESPONSE_REMOVED_MESSAGE
    return f"{message} Reasons: {', '.join(decision.reasons)}."
