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


"""Server-side GitHub App user-to-server token for a thread's git operations (`git_env`)."""

from __future__ import annotations

from dataclasses import dataclass

from app.auth.github.api import GITHUB_PROVIDER, parse_github_remote

NOT_CONNECTED_NOTICE = (
    "GitHub isn't connected, so private repositories can't be cloned or pushed. "
    "Connect GitHub in Settings → Apps."
)


@dataclass(frozen=True)
class SandboxGitHubToken:
    token: str = ""
    expires_at: float | None = None
    notice: str = ""


async def sandbox_github_token(repo: str) -> SandboxGitHubToken:
    from app.auth.oauth import ensure_fresh_access_token
    from app.auth.providers import provider_state
    from app.workers.manifest import repo_source

    if parse_github_remote(repo_source(repo)[0]) is None:
        return SandboxGitHubToken()
    token = await ensure_fresh_access_token(GITHUB_PROVIDER)
    if not token:
        return SandboxGitHubToken(notice=NOT_CONNECTED_NOTICE)
    state = provider_state(GITHUB_PROVIDER)
    return SandboxGitHubToken(
        token=token, expires_at=state.expires_at if state else None
    )
