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

from pydantic import BaseModel, Field

RUNTIME_PORT = 8080


class ExecRequest(BaseModel):
    command: str
    timeout: float | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class FileReadResponse(BaseModel):
    content_b64: str


class FileWriteRequest(BaseModel):
    content_b64: str
    mode: int | None = Field(
        default=None, description="Octal file mode (e.g. 0o644)"
    )


class ListEntry(BaseModel):
    name: str
    kind: str  # "file" | "dir" | "symlink"
    size: int
    mtime: int


class ListResponse(BaseModel):
    entries: list[ListEntry]
    truncated: bool


class ProcessSpawnRequest(BaseModel):
    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None


class ProcessSpawnResponse(BaseModel):
    session_id: str
    pid: int


class ProcessStatusResponse(BaseModel):
    session_id: str
    pid: int
    command: str
    is_running: bool
    exit_code: int | None = None
    output_size: int
    idle_seconds: float


class ProcessOutputResponse(BaseModel):
    data_b64: str
    next_offset: int
    exit_code: int | None = None


class ProcessStdinRequest(BaseModel):
    data_b64: str
    submit: bool = True


class ProcessListResponse(BaseModel):
    sessions: list[ProcessStatusResponse]


class ProcessKillResponse(BaseModel):
    exit_code: int
