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

"""FastAPI webhook entry point for the GitHub App."""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status

from .github_app import (
    GitHubAppClient,
    GitHubCommentTarget,
    extract_comment_command,
    is_supported_comment_event,
    verify_webhook_signature,
)
from .runner import run_agent

load_dotenv()

app = FastAPI(
    title="GitHub Gemini Enterprise App",
    description=(
        "GitHub App webhook that replies with answers grounded in a Gemini "
        "Enterprise datastore."
    ),
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Return a simple readiness response."""
    return {"status": "ok"}


@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(alias="X-Hub-Signature-256"),
) -> dict[str, str]:
    """Handle GitHub comment webhooks."""
    body = await request.body()
    webhook_secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    if not verify_webhook_signature(body, x_hub_signature_256, webhook_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature.",
        )

    payload = await request.json()
    if not is_supported_comment_event(x_github_event, payload):
        return {"status": "ignored"}

    command = extract_comment_command(
        payload,
        command_prefix=os.getenv("GITHUB_COMMAND_PREFIX", "@gemini-enterprise"),
        app_slug=os.getenv("GITHUB_APP_SLUG"),
    )
    if command is None:
        return {"status": "ignored"}

    installation_id = payload["installation"]["id"]
    repository_full_name = payload["repository"]["full_name"]
    comment_target = GitHubCommentTarget.from_event(x_github_event, payload)

    answer = await run_agent(command.prompt, context=command.context)
    github = GitHubAppClient.from_env()
    await github.post_comment(
        installation_id=installation_id,
        repository_full_name=repository_full_name,
        target=comment_target,
        body=answer,
    )

    return {"status": "answered"}
