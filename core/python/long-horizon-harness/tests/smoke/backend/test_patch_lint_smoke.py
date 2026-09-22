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

"""``edit`` fuzzy replacer + post-edit ruff diagnostics through the env interface.

Direct tool-call rather than LLM-driven — same pattern as
``test_terminal_tool.py`` / ``test_search_and_read_smoke.py``. The smoke
contract is twofold:

1. The fuzzy replacer ladder (``horizon/tools/_replacers.py``) resolves an
   ``oldText`` that differs from the file only by whitespace/indentation
   and applies the edit end-to-end through ``env.read_file`` /
   ``env.write_file`` — proving the ladder runs against the real backend.
2. Post-edit ruff diagnostics degrade cleanly: ``_post_edit_diagnostics``
   issues ``ruff check --output-format=json`` via ``env.execute`` and MUST
   NOT raise — when ruff is in the sandbox image the result carries a
   ``diagnostics`` string referencing the edited file; when ruff is absent
   the key is simply omitted. A missing-ruff sandbox image is an ACCEPTED
   degraded path (ship-ruff-in-the-image is a separate Dockerfile follow-up).

LLM behavior is covered by ``tests/eval``; the fuzzy-edit *agent* path is
covered by ``tests/smoke/backend/llm/test_agent_fuzzy_edit.py``.
"""

from __future__ import annotations

import pytest
from google.adk.environment import BaseEnvironment

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_fuzzy_patch_applies_whitespace_drifted_old_text(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import edit, read_file, write

    target = f"{unique_name}.py"
    # Original uses 4-space indentation inside the function body.
    original = "def greet(name):\n    return 'hi ' + name\n"
    write_result = await write(path=target, content=original)
    assert write_result["success"] is True

    # oldText drifts: leading/trailing whitespace + collapsed spacing.
    # The simple replacer misses; the fuzzy ladder (line_trimmed /
    # whitespace_normalized / indentation_flexible) must still resolve it.
    drifted_old = "  return 'hi '   + name  "
    patch_result = await edit(
        path=target,
        edits=[
            {"oldText": drifted_old, "newText": "    return 'hello ' + name"}
        ],
    )

    assert patch_result["success"] is True, patch_result
    assert patch_result["path"].endswith(target)
    # Real result key is ``replacements`` (NOT ``count``).
    assert patch_result["replacements"] == 1

    # The file content actually changed through the env write path.
    read_back = await read_file(path=target)
    assert read_back["success"] is True
    assert "hello " in read_back["content"]
    assert "hi " not in read_back["content"]


async def test_post_edit_ruff_never_raises_and_degrades_cleanly(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import write

    target = f"{unique_name}_lint.py"
    # An unused import — a classic ruff F401 finding. Whether ruff is present
    # in the sandbox image or not, the write itself must succeed: the
    # diagnostics path is best-effort and must never raise or fail the edit.
    content = "import os\n\n\ndef f():\n    return 1\n"

    result = await write(path=target, content=content)

    # No-raise contract: a successful, lint-clean-or-not write.
    assert result["success"] is True
    assert result["path"].endswith(target)

    diagnostics = result.get("diagnostics")
    if diagnostics is None:
        # Accepted degraded path: ruff missing from the sandbox image (or it
        # found nothing). Ship-ruff-in-the-image is a Dockerfile follow-up.
        pytest.skip("ruff produced no diagnostics (likely absent from image)")

    # When present, diagnostics is a STRING referencing the edited file.
    assert isinstance(diagnostics, str)
    assert "ruff diagnostics" in diagnostics
    assert target in diagnostics
