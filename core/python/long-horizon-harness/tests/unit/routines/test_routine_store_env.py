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
import yaml

from app.environment import LocalEnvironment
from app.routines.manifest import parse_manifest, write_routine_via_env

pytestmark = pytest.mark.asyncio


async def test_write_via_env_roundtrips_through_local_env(tmp_path):
    env = LocalEnvironment(working_dir=tmp_path)
    body = {
        "name": "Digest",
        "schedule": "0 8 * * *",
        "task": "summarize",
        "secrets": ["K"],
    }
    manifest = await write_routine_via_env(env, body, routine_id="digest")
    assert manifest.id == "digest"
    # the YAML landed under the env's working dir and re-parses to the same manifest
    written = (tmp_path / ".lha/routines/digest.yaml").read_text(
        encoding="utf-8"
    )
    assert (
        parse_manifest(yaml.safe_load(written), routine_id="digest") == manifest
    )


async def test_write_via_env_rejects_invalid(tmp_path):
    env = LocalEnvironment(working_dir=tmp_path)
    with pytest.raises(ValueError):
        await write_routine_via_env(
            env, {"name": "n"}, routine_id="x"
        )  # missing schedule/task
