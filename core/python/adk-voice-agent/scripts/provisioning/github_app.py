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

"""Create this deployment's GitHub App via GitHub's browser manifest flow."""

from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import socket
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.auth.github.api import GITHUB_API, GITHUB_PROVIDER, github_client
from app.auth.github.provider import GITHUB_APP_SLUG_ENV
from app.auth.providers import LOCAL_BASE_URL, require_provider
from scripts.provisioning import ui
from scripts.provisioning.common import redirect_uris

#: GitHub App names are at most 34 characters.
MAX_APP_NAME = 34
APP_NAME_PREFIX = "ADK Voice Agent"
APP_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
}
CREATED_PATH = "/created"
_ORG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def app_name(app_url: str | None) -> str:
    """`ADK Voice Agent (<host>)`, keeping the tail (project number) within GitHub's 34-char limit."""
    host = urlparse(app_url).hostname if app_url else socket.gethostname()
    label = (host or "local").split(".")[0]
    room = MAX_APP_NAME - len(f"{APP_NAME_PREFIX} ()")
    if len(label) > room:
        label = f"…{label[-(room - 1) :]}"
    return f"{APP_NAME_PREFIX} ({label})"


def app_manifest(app_url: str | None, redirect_url: str) -> dict[str, Any]:
    return {
        "name": app_name(app_url),
        "url": app_url or LOCAL_BASE_URL,
        "redirect_url": redirect_url,
        "callback_urls": redirect_uris(GITHUB_PROVIDER, app_url),
        "request_oauth_on_install": True,
        # Public so each user can install it on their own repos; only this deployment holds the client secret.
        "public": True,
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": [],
    }


def new_app_url(state: str, org: str | None = None) -> str:
    org = (org or "").strip()
    if org and not _ORG.fullmatch(org):
        raise ValueError(f"{org!r} is not a GitHub organization name.")
    path = (
        f"/organizations/{org}/settings/apps/new"
        if org
        else "/settings/apps/new"
    )
    return f"https://github.com{path}?{urlencode({'state': state})}"


def manifest_form_html(manifest: dict[str, Any], action: str) -> str:
    value = html.escape(json.dumps(manifest), quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8" /><title>Create the GitHub App</title></head>
<body>
  <form id="manifest" method="post" action="{html.escape(action, quote=True)}">
    <input type="hidden" name="manifest" value="{value}" />
    <noscript><button type="submit">Continue to GitHub</button></noscript>
  </form>
  <script>document.getElementById("manifest").submit();</script>
</body>
</html>"""


def parse_callback(query: str, state: str) -> str:
    params = parse_qs(query)
    if params.get("state", [""])[0] != state:
        raise ValueError("the redirect's state doesn't match this run.")
    code = params.get("code", [""])[0]
    if not _CODE.fullmatch(code):
        raise ValueError("GitHub returned no code, or a malformed one.")
    return code


class ManifestCatcher:
    """One-shot loopback HTTP server that serves the manifest form and catches GitHub's redirect."""

    def __init__(self, app_url: str | None, org: str | None = None) -> None:
        self.state = secrets.token_urlsafe(24)
        self.code: str | None = None
        self.error: str | None = None
        self._done = threading.Event()
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/"
        self.manifest = app_manifest(
            app_url, f"http://127.0.0.1:{port}{CREATED_PATH}"
        )
        self._form = manifest_form_html(
            self.manifest, new_app_url(self.state, org)
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path == "/":
                    self._reply(200, catcher._form)
                elif url.path == CREATED_PATH:
                    status, body = catcher.receive(url.query)
                    self._reply(status, body)
                else:
                    self._reply(404, "Not found")

            def _reply(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler

    def receive(self, query: str) -> tuple[int, str]:
        try:
            self.code = parse_callback(query, self.state)
        except ValueError as exc:
            self.error = str(exc)
            self._done.set()
            return 400, f"<p>Not created: {html.escape(self.error)}</p>"
        self._done.set()
        return (
            200,
            "<p>GitHub App created. You can close this tab and return to the terminal.</p>",
        )

    def wait(
        self,
        *,
        open_browser: Callable[[str], Any] = webbrowser.open,
        timeout: float = 600,
    ) -> str:
        thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        thread.start()
        try:
            open_browser(self.url)
            if not self._done.wait(timeout):
                raise ValueError(
                    f"no answer from GitHub within {int(timeout)} s."
                )
        finally:
            self._server.shutdown()
            self._server.server_close()
        if self.error or not self.code:
            raise ValueError(self.error or "GitHub returned no code.")
        return self.code


async def _convert(code: str) -> dict[str, Any]:
    async with github_client(timeout=30) as client:
        resp = await client.post(
            f"{GITHUB_API}/app-manifests/{code}/conversions"
        )
    if resp.status_code != 201:
        raise ValueError(
            f"GitHub returned {resp.status_code} creating the App: {resp.text[:200]}"
        )
    return resp.json()


def convert(code: str) -> dict[str, Any]:
    try:
        return asyncio.run(_convert(code))
    except httpx.HTTPError as exc:
        raise ValueError(f"could not reach GitHub: {exc}") from exc


def app_env(app: dict[str, Any]) -> dict[str, str]:
    missing = [
        k for k in ("slug", "client_id", "client_secret") if not app.get(k)
    ]
    if missing:
        raise ValueError(f"GitHub's answer lacks {', '.join(missing)}.")
    github = require_provider(GITHUB_PROVIDER)
    return {
        github.client_id_env: str(app["client_id"]),
        github.client_secret_env: str(app["client_secret"]),
        GITHUB_APP_SLUG_ENV: str(app["slug"]),
    }


def create_github_app(
    app_url: str | None,
    *,
    org: str | None = None,
    open_browser: Callable[[str], Any] = webbrowser.open,
    timeout: float = 600,
) -> dict[str, str]:
    catcher = ManifestCatcher(app_url, org)
    ui.action(
        1,
        "Your browser is opening GitHub. Review the App, then click Create GitHub App.",
        f"If it didn't open: {catcher.url}",
    )
    code = catcher.wait(open_browser=open_browser, timeout=timeout)
    return app_env(convert(code))
