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

from pathlib import Path

import pytest

from app.environment import Environment


class _Bare(Environment):
    @property
    def working_dir(self) -> Path:
        return Path("/workspace")

    async def execute(self, command, *, timeout=None):
        raise NotImplementedError

    async def read_file(self, path):
        raise NotImplementedError

    async def write_file(self, path, content):
        raise NotImplementedError

    async def list_directory(self, path, *, limit):
        return [], False

    async def delete_file(self, path, *, recursive=False):
        return None

    async def make_dir(self, path):
        return None

    async def download_zip(self, path):
        return b""

    async def upload_zip(self, path, data):
        return None

    async def spawn_process(self, command, *, cwd=None, env=None):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_refresh_auth_default_is_alive():
    assert await _Bare().refresh_auth() is True
