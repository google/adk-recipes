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

"""``before_model_callback`` that resolves the per-session model choice
and stamps it onto ``llm_request.model``.

Precedence: ``session.state["selected_model"]`` (set by ``/model``)
> ``LHA_ROOT_MODEL`` env > hardcoded ``DEFAULT_MODEL_NAME``.

Unknown values (stale state, typo in env) silently fall through to the
hardcoded default — the chat is more useful than a hard fail.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.context.compaction_threshold import compaction_token_threshold
from app.models.registry import (
    DEFAULT_MODEL_NAME,
    MODEL_REGISTRY,
    input_token_limit,
)

SELECTED_MODEL_STATE_KEY = "selected_model"

logger = logging.getLogger(__name__)


def _resolve(state: dict[str, Any] | None) -> str:
    if state:
        choice = state.get(SELECTED_MODEL_STATE_KEY)
        if choice in MODEL_REGISTRY:
            return choice
        if choice:
            logger.warning(
                "selected_model=%r not in registry; falling back", choice
            )

    env_choice = os.environ.get("LHA_ROOT_MODEL")
    if env_choice in MODEL_REGISTRY:
        return env_choice
    if env_choice:
        logger.warning(
            "LHA_ROOT_MODEL=%r not in registry; falling back", env_choice
        )

    return DEFAULT_MODEL_NAME


def resolve_model_name(state: dict[str, Any] | None) -> str:
    """Resolve the model a turn will run on — lets tools gate model-specific
    inputs (e.g. media types one backend can't ingest)."""
    return _resolve(state)


def apply_compaction_threshold(compaction_config: Any, model_name: str) -> None:
    """Stamp the percentage-of-window threshold onto the live compaction config.

    ADK reads ``token_threshold`` per turn from this shared object, so updating
    it here keeps the trigger aligned with the active model's window.
    """
    if compaction_config is None:
        return
    if getattr(compaction_config, "token_threshold", None) is None:
        # Token-threshold compaction not configured; nothing to align.
        return
    compaction_config.token_threshold = compaction_token_threshold(
        input_token_limit(model_name)
    )


def _compaction_config_from(callback_context: Any) -> Any:
    inv = getattr(callback_context, "_invocation_context", None)
    return getattr(inv, "events_compaction_config", None)


async def select_model_callback(
    *, callback_context: Any, llm_request: Any
) -> None:
    state = getattr(callback_context, "state", None)
    model_name = _resolve(state)
    llm_request.model = model_name
    apply_compaction_threshold(
        _compaction_config_from(callback_context), model_name
    )
