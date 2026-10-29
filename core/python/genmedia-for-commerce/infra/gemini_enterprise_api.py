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

"""Helpers of ``infra/gemini_enterprise.py``: gcloud, a minimal Discovery
Engine REST client, and the guided waits for the console-only steps."""

import json
import logging
import subprocess
import sys
import time
from collections.abc import Callable

import requests

from genmedia4commerce.core.errors import GenMediaError

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 60
OPERATION_TIMEOUT_S = 300
ENGINES = "collections/default_collection/engines"
CONSOLE = "https://console.cloud.google.com"
ENTER = "Press Enter once done (Ctrl+C to stop)... "
RERUN = "Then run make register-gemini-enterprise again to check."


class ApiError(GenMediaError):
    """A failed request, with its HTTP status and Google's error fields."""

    def __init__(self, method: str, url: str, status: int, text: str):
        super().__init__(f"{method} {url} failed: HTTP {status}\n{text}")
        self.status = status
        try:
            error = json.loads(text).get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        error = error if isinstance(error, dict) else {}
        self.message = error.get("message") or text
        self.reason = error.get("status", "")


class UserStepNeeded(Exception):
    """A step is left to the user (a console step nobody can confirm, as
    stdin is not a terminal, or the policy on custom MCP connectors); its
    instructions have been printed."""


def gcloud(*args: str) -> str:
    """Run gcloud and return its stdout."""
    result = subprocess.run(  # noqa: S603
        ["gcloud", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def console_url(page: str, project: str) -> str:
    """A Google Cloud console page of ``project``."""
    return f"{CONSOLE}/{page}?project={project}"


def wait_for_user(prompt: str, read=None, isatty=None) -> bool:
    """Wait for Enter; False at once when stdin is not a terminal (or is
    closed). Ctrl+C raises ``KeyboardInterrupt``."""
    if not (isatty or sys.stdin.isatty)():
        return False
    try:
        (read or input)(prompt)
    except EOFError:
        return False
    return True


def poll(check: Callable[[], bool], interval_s: float, timeout_s: float):
    """Call ``check`` until it returns True, every ``interval_s`` seconds for
    up to ``timeout_s``; True when it did."""
    attempts = max(1, int(timeout_s // interval_s))
    for attempt in range(attempts):
        if attempt:
            time.sleep(interval_s)
        if check():
            return True
    return False


def guide_user(step: str, done: Callable[[], bool], retry: str, prompt=ENTER):
    """Print a console step, then wait for Enter and check ``done`` until it
    is True. False (after telling to run the command again) when stdin is
    not a terminal."""
    logger.warning(step)
    while wait_for_user(prompt):
        if done():
            return True
        logger.warning(retry)
    logger.warning(RERUN)
    return False


class DiscoveryEngine:
    """Minimal Discovery Engine REST client; writes are only logged in a dry run."""

    def __init__(self, project: str, location: str, token: str, dry_run: bool):
        self.host = (
            "discoveryengine.googleapis.com"
            if location == "global"
            else f"{location}-discoveryengine.googleapis.com"
        )
        self.project = project
        self.parent = f"projects/{project}/locations/{location}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Goog-User-Project": project,
        }
        self.dry_run = dry_run

    def call(self, method, path, body=None, version="v1alpha") -> dict:
        """One request; ``ApiError`` with the response body on failure."""
        url = f"https://{self.host}/{version}/{path}"
        if method != "GET" and self.dry_run:
            logger.info("[dry run] %s %s %s", method, url, body or "")
            return {}
        response = requests.request(
            method, url, headers=self.headers, json=body, timeout=HTTP_TIMEOUT_S
        )
        if not response.ok:
            raise ApiError(method, url, response.status_code, response.text)
        return response.json() if response.content else {}

    def get_or_none(self, path: str) -> dict | None:
        """GET ``path``; None when it does not exist (HTTP 404)."""
        try:
            return self.call("GET", path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def wait(self, operation: dict) -> None:
        """Poll a long-running operation until it is done."""
        deadline = time.monotonic() + OPERATION_TIMEOUT_S
        while operation.get("name") and not operation.get("done"):
            if time.monotonic() > deadline:
                raise GenMediaError(f"{operation['name']} did not finish")
            time.sleep(5)
            operation = self.call("GET", operation["name"])
        if "error" in operation:
            raise GenMediaError(f"operation failed: {operation['error']}")
