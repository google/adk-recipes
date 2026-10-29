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

"""The organisation policy on custom MCP connectors, for
``infra/gemini_enterprise.py``.

Gemini Enterprise blocks custom MCP connectors by default (``POLICY``). When
it refuses the Studio's connector, the registration asks, in a terminal, to
allow them for the project (a per-project override, ``enforce: false``) and
retries the connector while the change applies; without a terminal it prints
how. ``make allow-mcp-connectors`` sets the same override on its own."""

import contextlib
import logging
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

from infra.gemini_enterprise_api import RERUN, UserStepNeeded, gcloud

logger = logging.getLogger(__name__)

POLICY = "discoveryengine.managed.disableCustomMcpServerConnector"
# A changed policy takes a minute or two to apply: retry the connector every
# POLICY_RETRY_S for up to POLICY_WAIT_S.
POLICY_RETRY_S = 15
POLICY_WAIT_S = 180

QUESTION = """
Gemini Enterprise blocks custom MCP connectors by default (org policy
{policy}), and the Studio panel needs one.
Allowing them for project {project} lets anyone who manages Gemini Enterprise in this project add any custom
MCP server, not only this one. It needs the Organization Policy Administrator role."""
ASK = "\nAllow custom MCP connectors for project {project}? [Y/n] "
APPLYING = "Policy changed. Waiting for it to apply (usually 1-2 minutes)..."
STILL_BLOCKED = "  still blocked, trying again in %d s (%d:%02d)"
NOT_APPLIED = (
    "The policy is set but not applied yet: run make "
    "register-gemini-enterprise again in a minute."
)
DECLINED = (
    "Policy not changed. To allow it later: make allow-mcp-connectors, "
    "then run make register-gemini-enterprise again."
)
DENIED = """Could not change the policy (needs the Organization Policy Administrator role). Ask an admin to run:
{steps}
then run make register-gemini-enterprise again."""
BLOCKED_STEPS = """
Custom MCP connectors are blocked ({policy}; Gemini Enterprise blocks them by default). Someone with the Organization Policy Administrator role can allow them for this project only with make allow-mcp-connectors (the change takes a minute or two to apply); as that allows any custom MCP connector in the project, prefer a project dedicated to GenMedia for Commerce and its Gemini Enterprise app. Or by hand:
{steps}"""
ALLOWED = (
    "Policy changed: custom MCP connectors are allowed in project {project} "
    "({policy}, enforce: false). It takes a minute or two to apply; then "
    "run make register-gemini-enterprise."
)


def policy_yaml(project: str) -> str:
    """The per-project override that allows custom MCP connectors."""
    return (
        f"name: projects/{project}/policies/{POLICY}\n"
        "spec:\n  rules:\n  - enforce: false\n"
    )


def manual_steps(project: str) -> str:
    """The two commands that set the override by hand."""
    printf = policy_yaml(project).replace("\n", "\\n")
    return (
        f"  printf '{printf}' > policy.yaml\n"
        f"  gcloud org-policies set-policy policy.yaml --project={project}"
    )


def blocked_steps(project: str) -> str:
    """What to do when the policy blocks the connector and nobody can be
    asked."""
    return BLOCKED_STEPS.format(policy=POLICY, steps=manual_steps(project))


def interactive() -> bool:
    """Whether someone can answer: stdin and stdout are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(project: str) -> bool:
    """Ask to allow custom MCP connectors; Enter, y or yes is a yes."""
    logger.warning(QUESTION.format(policy=POLICY, project=project))
    try:
        answer = input(ASK.format(project=project))
    except EOFError:
        return False
    return answer.strip().lower() in ("", "y", "yes")


def allow(project: str) -> bool:
    """Set the per-project override. False, after printing what an admin
    must run, when permission is denied; other gcloud errors raise."""
    with contextlib.suppress(subprocess.CalledProcessError):
        # Best effort: set-policy says so when the API stays disabled.
        gcloud(
            "services",
            "enable",
            "orgpolicy.googleapis.com",
            f"--project={project}",
            "--quiet",
        )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
        file.write(policy_yaml(project))
        file.flush()
        try:
            gcloud(
                *("org-policies", "set-policy", file.name),
                f"--project={project}",
                "--quiet",
            )
        except subprocess.CalledProcessError as exc:
            if "permission" not in (exc.stderr or "").lower():
                raise
            logger.error(DENIED.format(steps=manual_steps(project)))
            return False
    return True


def wait_until_applied(created: Callable[[], bool]) -> bool:
    """Retry ``created`` every POLICY_RETRY_S for up to POLICY_WAIT_S;
    True once it passed."""
    logger.info(APPLYING)
    waited = 0
    while waited < POLICY_WAIT_S:
        time.sleep(POLICY_RETRY_S)
        waited += POLICY_RETRY_S
        if created():
            return True
        if waited < POLICY_WAIT_S:
            logger.info(STILL_BLOCKED, POLICY_RETRY_S, *divmod(waited, 60))
    logger.error(NOT_APPLIED)
    return False


def unblock(project: str, created: Callable[[], bool]) -> None:
    """The policy refused the connector: in a terminal, ask to allow custom
    MCP connectors in ``project``, then retry ``created`` until the change
    applies; otherwise print how to allow them. ``UserStepNeeded`` when the
    connector is still not created (what to do has been printed)."""
    if not interactive():
        logger.warning(blocked_steps(project))
        logger.warning(RERUN)
        raise UserStepNeeded
    if not ask(project):
        logger.warning(DECLINED)
        raise UserStepNeeded
    if not (allow(project) and wait_until_applied(created)):
        raise UserStepNeeded


def allow_mcp_connectors_in(project: str) -> int:
    """``make allow-mcp-connectors``: set the override; the exit status."""
    try:
        if not allow(project):
            return 1
    except subprocess.CalledProcessError as exc:
        logger.error("Error: gcloud %s\n%s", " ".join(exc.cmd[1:]), exc.stderr)
        return 1
    logger.info(ALLOWED.format(project=project, policy=POLICY))
    return 0
