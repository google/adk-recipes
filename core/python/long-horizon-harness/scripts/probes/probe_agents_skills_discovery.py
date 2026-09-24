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

"""Live smoke: a skill staged where `npx skills add` puts it is auto-discovered.

Seeds a distinctive skill under ``<workspace>/.agents/skills/<name>/`` (the
Skills CLI staging location) — NOT under ``skills/`` — then drives the REAL
root agent (real Gemini). Proves the whole chain:

  1. code-level: the skill appears in ``bound_skill_catalog()`` after the
     session-start mirror (i.e. ``.agents/skills`` is now a discovered root),
  2. live-model: the model can read a UNIQUE token from that skill's injected
     ``<available_skills>`` catalog entry — proving it's actually surfaced to
     the model, not just on disk.

Run:  (env from .env)  uv run python scripts/probes/probe_agents_skills_discovery.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set. LHA_ENVIRONMENT_BACKEND=local.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from google.genai import types

_SKILL_NAME = "probe-weather-oracle"
_UNIQUE_TOKEN = "MANGO-42-FORECAST"  # noqa: S105  # gitleaks:allow
_SKILL_MD = (
    f"---\nname: {_SKILL_NAME}\n"
    f"description: Predicts weather using the {_UNIQUE_TOKEN} protocol.\n---\n"
    f"# {_SKILL_NAME}\n\nInternal codeword: {_UNIQUE_TOKEN}.\n"
)

_PROMPT = (
    "Look at your <available_skills> catalog. Is there a skill named "
    f"'{_SKILL_NAME}'? If yes, reply with EXACTLY the codeword that appears in "
    "its description and nothing else. If it is not there, reply 'ABSENT'."
)

_USER_ID = "probe-user"


async def main() -> None:
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import set_environment_provider
    from horizon.fast_api_app import build_runner
    from horizon.tools.skill_reload import bound_skill_catalog

    workspace = Path(tempfile.mkdtemp(prefix="lha-skill-probe-"))
    # Stage ONLY under .agents/skills/ — the npx location, not skills/.
    staged = workspace / ".agents" / "skills" / _SKILL_NAME
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "SKILL.md").write_text(_SKILL_MD)
    print(f"seeded {_SKILL_NAME} at {staged}")

    # Bring-your-own env override → the run uses OUR workspace as working_dir.
    set_environment_provider(
        lambda _uid: LocalEnvironment(working_dir=workspace)
    )
    try:
        runner = build_runner()
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=_USER_ID
        )

        reply = ""
        async for event in runner.run_async(
            user_id=_USER_ID,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=_PROMPT)]
            ),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        reply = part.text

        catalog = {s["name"] for s in bound_skill_catalog()}
        discovered = _SKILL_NAME in catalog
        model_saw = _UNIQUE_TOKEN in (reply or "")

        print(f"\nbound catalog contains {_SKILL_NAME!r}: {discovered}")
        print(f"model reply: {reply!r}")
        print(f"model read the unique token: {model_saw}")
        ok = discovered and model_saw
        print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    finally:
        set_environment_provider(None)


if __name__ == "__main__":
    asyncio.run(main())
