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

"""The `navigate` voice tool: opens home, a project, a thread, or a connected app's page.

The UI watches `/run_live` for `navigate` tool calls and switches
the stage on the spot; `set_focus` updates `OPEN ON SCREEN` server-side so follow-up
turns resolve pronouns ("this", "it") to what was opened.
"""

from __future__ import annotations

from app.auth.providers import configured_apps
from app.projects import project_thread
from app.threads.registry import get_thread_registry
from app.voice.focus import set_focus


async def navigate(
    target: str = "home",
    project: str = "",
    thread_id: str = "",
    app: str = "",
) -> str:
    """Open home, a project, a thread, or a connected app's Connect page on screen.

    Args:
        target: 'home', 'project', 'thread', or 'app'.
        project: For 'project': its folder ('projects/<slug>', 'code/<repo>', 'scratch/<slug>').
        thread_id: For 'thread': its ID.
        app: For 'app': its ID ('google_workspace', 'github').
    """
    kind = target.strip().lower() or "home"
    chosen = (thread_id or project).strip()
    if kind == "home":
        set_focus(view="home", notify=False)
        return "Showing home."
    if kind in ("thread", "project"):
        if chosen:
            handle = await get_thread_registry().get(chosen)
            if handle is not None:
                set_focus(
                    view="thread",
                    project=handle.project,
                    thread_id=handle.thread_id,
                    notify=False,
                )
                return (
                    f"Opened {handle.title} ({handle.thread_id}) in {handle.project} — "
                    f"status: {handle.status}."
                )
        found = await project_thread(chosen)
        if found is None:
            return f"Couldn't open thread {chosen!r}: no such project. Use list_projects or YOUR WORK for the ID."
        set_focus(view="project", project=chosen, notify=False)
        return (
            f"Opened {found['project']['title']} ({chosen}). Use the catch-up line to say "
            f"where it stands in one sentence.\n{found['voice_context']}"
        )
    if kind == "app":
        known = {a.name: a for a in configured_apps()}
        if app not in known:
            return f"Couldn't open app {app!r}: choose from {', '.join(known)}."
        return f"Opened {known[app].display_name} on screen so the user can connect it."
    return f"Unknown navigation target {target!r}: use 'home', 'thread', 'project', or 'app'."
