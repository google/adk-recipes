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

"""Per-session compaction context — memory-service handles for the summarizer.

``EventsCompactionConfig`` is set on the ``App`` at module load, before
any session exists, so the summarizer must defer per-session state lookup
to call time. This ContextVar is set in ``on_session_start_callback``
once the invocation context is known and read by ``HorizonSummarizer`` when
deciding whether to fire the pre-compaction flush fork.

Mirrors the shape of ``app/environment_context.py`` so the same hermetic
teardown pattern applies in tests.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class CompactionContext:
    memory_service: Any
    app_name: str
    user_id: str
    sibling_agent_plugin: Any = None


_active_compaction_context: ContextVar[CompactionContext | None] = ContextVar(
    "lha_active_compaction_context", default=None
)


def bind_compaction_context(ctx: CompactionContext) -> None:
    _active_compaction_context.set(ctx)


def clear_compaction_context() -> None:
    _active_compaction_context.set(None)


def get_compaction_context() -> CompactionContext | None:
    return _active_compaction_context.get()
