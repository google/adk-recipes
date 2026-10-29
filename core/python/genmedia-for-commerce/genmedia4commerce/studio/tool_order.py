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

"""When the agent's ``generate`` may start a job.

The agent never generates what the user has not seen: ``generate`` refuses
(``needs_confirmation``) when the agent changed in the current turn the
studio's inputs, its page or a setting that changes the cost, or asks for
another page than the one the turn started on, so the model asks "Shall I
generate?" and generates on the user's next message. Inputs are the person,
the reference model, the products and the spin photos: an ``INPUT_OPS``
patch. ``remake_video`` (a "Make video" of one try-on image) goes through
the same guard. A page switch (a
``capability`` patch: ``open_capability`` records one only when the page
changes) can make the generation a much dearer video; so can more
variations or a higher resolution (``COST_SETTINGS``). Other settings (scene,
aspect ratio...) do not count. What the user changes in the panel is not a
patch of the turn, so it never counts: a bare "generate" or "again" with
the studio as the user last saw it starts at once.

ADK runs the calls of one model response at once, so ``generate`` could
check before ``choose_person`` of the same response has changed anything.
The async setup tools are marked ``setup_tool`` and count as changing the
inputs while they run; the sync tools record their patches before they
return. ``needs_confirmation`` first lets the other calls of the response
start. Two ``generate`` calls of one response would both find no job
running: ``start_once`` lets only one start.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any

from genmedia4commerce.studio import turn

__all__ = [
    "COST_SETTINGS",
    "INPUT_OPS",
    "needs_confirmation",
    "setup_tool",
    "start_once",
]

#: The patch ops that change a generation's inputs.
INPUT_OPS = frozenset({"set", "add", "remove", "clear"})
#: The patch ops a generation needs the user's confirmation after.
_CONFIRM_OPS = INPUT_OPS | {"capability"}
#: The settings that change what a generation costs.
COST_SETTINGS = frozenset({"num_variations", "resolution", "image_size"})

#: How long ``generate`` lets the other calls of its model response start.
_GRACE_S = 0.05


def setup_tool(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    """Count an async tool as changing the studio's inputs while it runs."""

    @functools.wraps(fn)
    async def run(*args: Any, **kwargs: Any) -> Any:
        sc = turn.current()
        sc.setting_up += 1
        try:
            return await fn(*args, **kwargs)
        finally:
            sc.setting_up -= 1

    return run


def _needs_asking(p: dict[str, Any]) -> bool:
    """Whether a patch of the turn changes what a generation makes or
    costs."""
    settings = p.get("settings")
    costly = isinstance(settings, dict) and not COST_SETTINGS.isdisjoint(
        settings
    )
    return costly or not _CONFIRM_OPS.isdisjoint(p)


async def needs_confirmation(capability: str) -> bool:
    """Whether ``generate`` on page ``capability`` must ask the user first,
    once the other calls of the model response have started: the agent
    changed (or is changing) the inputs, the page or a cost setting in this
    turn, or ``capability`` is not the page the turn started on (without a
    page switch, the working state's page is that one)."""
    await asyncio.sleep(_GRACE_S)
    sc = turn.current()
    if sc.setting_up > 0 or any(map(_needs_asking, sc.out.patches)):
        return True
    return capability != str(sc.st.get("capability") or "tryon")


async def start_once(
    job_status: Callable[[], Awaitable[dict]],
    start: Callable[[], Awaitable[dict]],
) -> dict | None:
    """Start a job (``start``) unless the studio's job (``job_status``) is
    running or another ``generate`` of the turn is starting one: what
    ``start`` returned, or ``None`` when refused. The caller puts the job in
    the working state without awaiting first, so a later check finds it
    running. Errors of ``start`` are raised."""
    sc = turn.current()
    if sc.starting:
        return None
    sc.starting = True
    try:
        if (await job_status()).get("status") == "running":
            return None
        return await start()
    finally:
        sc.starting = False
