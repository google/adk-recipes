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

"""Cross-session listing of a user's memories for the chat UI.

Surfaces both marker-tagged ``memory`` writes and Memory Bank's
distilled (unmarked) facts, and pins the structured profile to the top of
the first page.
"""

from __future__ import annotations

from google.adk.memory import BaseMemoryService

# Scope/MemoryWriteEntry/parse_marker/_FETCH_CAP now live behind the memory
# adapter boundary; re-exported here for callers/tests that referenced them.
from horizon.memory.adapter import (  # noqa: F401
    _FETCH_CAP,
    MemoryListResult,
    MemoryWriteEntry,
    Scope,
    memory_adapter,
    parse_marker,
)
from horizon.memory.user_profile import load_user_profile


async def list_memory_writes(
    *,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    limit: int,
    offset: int,
) -> MemoryListResult:
    all_entries = await memory_adapter(memory_service).list_all(
        app_name=app_name, user_id=user_id
    )

    # Stable sort: pre-reversing then sort(reverse=True) on equal keys yields
    # last-inserted-first, which is the tiebreak we want when many events
    # share the same float `timestamp`.
    all_entries.reverse()
    all_entries.sort(key=lambda e: e.ts_ms, reverse=True)

    counts = {"user": 0, "agent": 0, "user_profile": 0}
    for entry in all_entries:
        counts[entry.scope] = counts.get(entry.scope, 0) + 1

    page = all_entries[offset : offset + limit]
    has_more = (offset + limit) < len(all_entries)

    # Pin the structured profile to the top of the first page so the panel
    # shows it alongside recent memories ("we have a profile"). It lives behind
    # retrieve_profiles, not in the memory list, so it is fetched separately.
    if offset == 0:
        profile = await load_user_profile(
            memory_service=memory_service,
            app_name=app_name,
            user_id=user_id,
            include_lists=True,
        )
        if profile:
            newest_ts = max((e.ts_ms for e in all_entries), default=0)
            page.insert(
                0,
                MemoryWriteEntry(
                    scope="user_profile",
                    content=profile,
                    ts_ms=newest_ts,
                    session_id=None,
                ),
            )
            counts["user_profile"] = counts.get("user_profile", 0) + 1

    return MemoryListResult(entries=page, counts=counts, has_more=has_more)
