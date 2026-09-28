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

import pytest

from app.sandbox.runtime.protocol import ExecRequest
from app.sandbox.runtime.server import exec_command


@pytest.mark.asyncio
async def test_exec_passes_injected_env_to_subprocess():
    req = ExecRequest(command='echo "$LHA_SPIKE"', env={"LHA_SPIKE": "ok"})
    resp = await exec_command(req)
    assert resp.exit_code == 0
    assert resp.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_exec_without_env_still_runs():
    resp = await exec_command(ExecRequest(command="echo hi"))
    assert resp.exit_code == 0
    assert resp.stdout.strip() == "hi"
