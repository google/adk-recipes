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

"""LLM-driven: agent recovers a needle from overflowed terminal output.

A command's oversized output is spilled by ``terminal`` to ``lha/tool-output/``
with only a preview + pointer returned. The needle sits past the preview, so the
agent must follow the pointer (``view_file`` / ``read_file`` / grep) to answer.
The overflow is triggered in setup (a capable model would otherwise grep/tail to
avoid overflowing); this exercises the recovery half end-to-end.

Assertions target durable side effects + the needle in the final answer —
never model phrasing. The needle is a deterministic token we planted, not free
text — see the project agent guidance: "Never assert on LLM output content in
pytest."
"""

from __future__ import annotations

import pytest
from google.adk.environment import BaseEnvironment

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _final_text(events: list) -> str:
    """Concatenate all model-authored text across the turn's events."""
    chunks: list[str] = []
    for event in events:
        content = getattr(event, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


async def test_agent_recovers_needle_from_overflow_file(
    llm_runner, active_env: BaseEnvironment
) -> None:
    needle = "NEEDLE_a1b2c3d4e5"
    # We trigger the overflow ourselves rather than ask the agent to run a big
    # command: a capable model satisfies "find the NEEDLE line" by piping to
    # grep/tail, so it never overflows. That the agent-run path spills is proven
    # deterministically in test_terminal_overflow_smoke.py; here we test the
    # RECOVERY half — given a truncated result + a saved-file pointer, the agent
    # must read the file to surface a needle that is past the preview.
    from app.tools.processes.terminal import terminal

    command = (
        "python3 -c \"[print('filler-%06d' % i) for i in range(15000)]; "
        f"print('{needle}'); "
        "[print('filler-%06d' % i) for i in range(15000, 30000)]\""
    )
    result = await terminal(command=command)
    pointer = result["stdout"]
    assert result["truncated"] is True
    # Needle is in the omitted MIDDLE — absent from the head+tail preview, so
    # the agent can only get it by reading the saved file.
    assert needle not in pointer

    prompt = (
        "A previous command produced large output, shown here truncated with a "
        "pointer to where the full output was saved:\n\n"
        f"{pointer}\n\n"
        f"Read the saved file and tell me the exact line containing '{needle}'."
    )

    events = await llm_runner.run_once(prompt)

    # The agent followed the pointer (view_file/read_file) and surfaced the needle.
    assert needle in _final_text(events), (
        "agent did not recover the needle from the saved overflow file"
    )
