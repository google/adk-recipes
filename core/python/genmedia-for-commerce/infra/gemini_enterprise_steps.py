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

"""The custom MCP connector and its actions, for ``infra/gemini_enterprise.py``.

Every step can run again: the connector is reused when one serves the same
URL, and activating or enabling actions is idempotent. When the organisation
policy blocks custom MCP connectors, it asks to allow them
(``gemini_enterprise_policy.unblock``); when Gemini Enterprise has not read
the Studio's tools yet ("Reload custom actions", console-only), it prints the
step and waits for the user (``guide_user``)."""

import logging
import time

from genmedia4commerce.core.errors import GenMediaError
from infra.gemini_enterprise_api import (
    OPERATION_TIMEOUT_S,
    DiscoveryEngine,
    console_url,
    guide_user,
    poll,
)
from infra.gemini_enterprise_policy import POLICY, unblock

logger = logging.getLogger(__name__)

DISPLAY_NAME = "GenMedia Studio"
# The tools Gemini Enterprise's model sees (mcp/studio.py marks the panel's
# tools app-only, so the console lists only studio_agent: the Studio's action).
STUDIO_ACTIONS = ["studio_agent"]

RELOAD_STEP = """
Last step, in the console (it has no public API yet): Gemini Enterprise > Data stores > {name} ({cid}) > Actions > Reload custom actions, then enable {actions}.
  {url}"""


def action_config(mcp_url: str) -> dict:
    return {
        "isActionConfigured": True,
        "createBapConnection": True,
        "actionParams": {"auth_type": "NO_AUTH", "instance_uri": mcp_url},
    }


def connector_request(mcp_url: str, collection_id: str) -> dict:
    """Body of ``:setUpDataConnector`` for a custom MCP server."""
    return {
        "collectionId": collection_id,
        "collectionDisplayName": DISPLAY_NAME,
        "dataConnector": {
            "dataSource": "custom_mcp",
            "dataSourceVersion": 1,
            # Required by the API but not used with NO_AUTH; the value is the
            # placeholder Google's custom MCP codelab sends.
            "params": {"oauth_access_token": "unused"},
            "refreshInterval": "86400s",
            "entities": [{"entityName": "mcp_data"}],
            "connectorModes": ["FEDERATED", "ACTIONS"],
            "actionConfig": action_config(mcp_url),
        },
    }


def policy_blocked(exc: Exception) -> bool:
    """Whether the organisation policy refused the custom MCP connector."""
    text = str(exc)
    return POLICY in text or "CUSTOM_ORG_POLICY_VIOLATION" in text


def find_collection(api: DiscoveryEngine, mcp_url: str) -> str | None:
    """The id of a "GenMedia Studio" collection already serving ``mcp_url``."""
    listed = api.call("GET", f"{api.parent}/collections")
    for collection in listed.get("collections", []):
        if collection.get("displayName") != DISPLAY_NAME:
            continue
        action = collection.get("dataConnector", {}).get("actionConfig", {})
        if action.get("actionParams", {}).get("instance_uri") == mcp_url:
            return collection["name"].rsplit("/", 1)[-1]
    return None


def create_collection(api: DiscoveryEngine, mcp_url: str) -> str:
    """Create the custom MCP connector (and its collection); return its id.
    While the organisation policy blocks it, ask to allow it (``unblock``)."""
    cid = f"genmedia-studio-{int(time.time())}-collection"
    logger.info("Creating the custom MCP data connector %s", cid)
    body = connector_request(mcp_url, cid)

    def created() -> bool:
        try:
            api.wait(api.call("POST", f"{api.parent}:setUpDataConnector", body))
        except GenMediaError as exc:
            if not policy_blocked(exc):
                raise
            return False
        return True

    if not created():
        unblock(api.project, created)
    return cid


def activate(api: DiscoveryEngine, cid: str, mcp_url: str) -> None:
    """Turn on the connector's actions. Idempotent, so it also runs for a
    reused connector (an earlier run may have stopped before it)."""
    logger.info("Activating the actions of %s", cid)
    path = f"{api.parent}/collections/{cid}/dataConnector"
    body = {"name": path, "actionConfig": action_config(mcp_url)}

    def patched() -> bool:
        try:
            api.call("PATCH", f"{path}?updateMask=actionConfig", body)
        except GenMediaError as exc:
            if "INITIALIZING" not in str(exc):
                raise
            logger.info("Waiting for %s to finish initializing...", cid)
            return False
        return True

    # A new connector refuses updates while it is INITIALIZING (up to about a
    # minute), even when it already reports state ACTIVE: retry until then.
    if not poll(patched, 10, OPERATION_TIMEOUT_S):
        raise GenMediaError(f"{cid} is still initializing")


def data_store_ids(api: DiscoveryEngine, cid: str) -> list[str]:
    """The ids of the connector's data stores (in default_collection; the
    connector names them in ``entities[].dataStore``)."""
    path = f"{api.parent}/collections/{cid}/dataConnector"
    entities = api.call("GET", path).get("entities", [])
    ids = [
        e["dataStore"].rsplit("/", 1)[-1]
        for e in entities
        if e.get("dataStore")
    ]
    if not ids:
        raise GenMediaError(f"collection {cid} has no data store yet")
    return ids


def enable_actions(api: DiscoveryEngine, cid: str) -> None:
    """Enable the Studio's action (the console's "Enable actions")."""
    path = f"{api.parent}/collections/{cid}/dataConnector"
    logger.info("Enabling the Studio's action %s", ", ".join(STUDIO_ACTIONS))
    api.call(
        "PATCH",
        f"{path}?updateMask=bapConfig",
        {"bapConfig": {"enabledActions": STUDIO_ACTIONS}},
    )


def actions_loaded(api: DiscoveryEngine, cid: str) -> bool:
    """Whether Gemini Enterprise has read the Studio's action, and no other
    tool (an older version's tools stay until the next reload)."""
    path = f"{api.parent}/collections/{cid}/dataConnector"
    tools = api.call("GET", path).get("dynamicTools") or []
    return sorted(t.get("name", "") for t in tools) == sorted(STUDIO_ACTIONS)


def wait_for_actions(api: DiscoveryEngine, cid: str) -> bool:
    """Make sure the Studio's action is loaded: reading the tools ("Reload
    custom actions", ``refreshDataConnectorTools``) is console-only, so guide
    the user until ``dynamicTools`` lists it. False when stdin is not a
    terminal (the step is printed; the rest of the registration is done)."""
    actions = ", ".join(STUDIO_ACTIONS)
    if not actions_loaded(api, cid):
        url = console_url("gemini-enterprise/data-stores", api.project)
        step = RELOAD_STEP.format(
            name=DISPLAY_NAME, cid=cid, actions=actions, url=url
        )
        retry = f"Gemini Enterprise does not list {actions} yet."
        if not guide_user(step, lambda: actions_loaded(api, cid), retry):
            return False
        enable_actions(api, cid)  # idempotent: enabled even if not ticked
    logger.info("The Studio's action %s is loaded and enabled.", actions)
    return True
