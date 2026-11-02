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

"""The Gemini Enterprise app, for ``infra/gemini_enterprise.py``.

By default the command creates its own app, "GenMedia Studio", with the
Studio's data store linked at creation. That is the only way that works on a
new app: the API cannot add the first data store to an app that has none
(``PATCH dataStoreIds`` answers FAILED_PRECONDITION "can not remove data
stores such that <2 data stores remain linked" in every API version, and
``data_stores`` is immutable), while adding one to an app that already has one
works. The app id is stable, so a second run finds and reuses it.

Our app's assistant (its chat) gets Google Search (web grounding) off, the
model GENMEDIA_ASSISTANT_MODEL and instructions to only relay messages to
and from the Studio's agent, which does the work; an app named in
GEMINI_ENTERPRISE_APP_ID keeps its own settings."""

import logging

import requests

from genmedia4commerce.core.errors import GenMediaError
from genmedia4commerce.core.models import get_models
from infra.gemini_enterprise_api import (
    ENGINES,
    ApiError,
    DiscoveryEngine,
    UserStepNeeded,
    console_url,
    guide_user,
)
from infra.gemini_enterprise_steps import DISPLAY_NAME

logger = logging.getLogger(__name__)

# Words of the errors of a project without Gemini Enterprise (no
# subscription, licence or free trial). Matched loosely, as the API's wording
# is not documented; the API's own message is printed too.
SUBSCRIPTION_HINTS = (
    "subscription",
    "licence",
    "license",
    "billing",
    "trial",
    "not enabled",
)

NOT_ENABLED = """
Gemini Enterprise does not seem to be enabled for project {project}, so the app cannot be created (the API's message is above).
Enable Gemini Enterprise for this project: console > Gemini Enterprise (start the subscription or free trial):
  {url}"""

# Our app's chat only relays: the Studio has its own agent.
RELAY = """You are only a relay between the user and the GenMedia Studio, which has its own expert agent. You do no work yourself.
- For every user message, call the studio_agent tool right away, with the user's message exactly as written, word for word (even a single word like "yes"), and the studio_id from the studio's model context (empty if there is none yet). Do not rephrase, summarise, add context, plan or think it over first: the Studio's agent does all of that.
- Then reply with the text of the tool's "reply" exactly as it is, word for word, and nothing else. If the tool returns an "error" instead, reply with that error text as it is.
- Never write anything of your own: no greeting, no "I'm sending this to the Studio", no summary, no explanation of what the tool did, no follow-up question.
- Never answer from your own knowledge, never create or describe images or videos, and never use any other tool."""
ASSISTANT_MASK = (
    "webGroundingType,generationConfig.defaultModelId,"
    "generationConfig.systemInstruction.additionalSystemInstruction"
)
NOT_SET = (
    "Could not set up the app's chat (%s): Google Search stays available "
    "there and it uses the default model and instructions. Change them in "
    "the app's settings in the console."
)

CONNECT_STEP = """
The API cannot add the first data store to an existing app (it answered: {message}).
Connect it in the console: Gemini Enterprise > Apps > {app} > Connected data stores > add {name} (data store {stores}).
  {url}"""


def app_request(stores: list[str]) -> dict:
    """Body of ``engines.create`` for a Gemini Enterprise app."""
    return {
        "displayName": DISPLAY_NAME,
        "solutionType": "SOLUTION_TYPE_SEARCH",
        "industryVertical": "GENERIC",
        "appType": "APP_TYPE_INTRANET",
        "searchEngineConfig": {
            "searchTier": "SEARCH_TIER_ENTERPRISE",
            "searchAddOns": ["SEARCH_ADD_ON_LLM"],
            "requiredSubscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
        },
        "dataStoreIds": stores,
    }


def subscription_missing(exc: Exception) -> bool:
    """Whether an error says Gemini Enterprise is not enabled."""
    text = str(exc).lower()
    return any(hint in text for hint in SUBSCRIPTION_HINTS)


def engine_name(api: DiscoveryEngine, app_id: str) -> str:
    """The engine resource name of a full name or an id."""
    return app_id if "/" in app_id else f"{api.parent}/{ENGINES}/{app_id}"


def create_app(api: DiscoveryEngine, engine_id: str, stores: list[str]):
    """Create our app with ``stores`` linked; while the project has no
    Gemini Enterprise, guide the user to enable it."""
    path = f"{api.parent}/{ENGINES}?engineId={engine_id}"
    body = app_request(stores)

    def created() -> bool:
        try:
            api.wait(api.call("POST", path, body))
        except GenMediaError as exc:
            if not subscription_missing(exc):
                raise
            logger.warning("The API said: %s", getattr(exc, "message", exc))
            return False
        return True

    logger.info("Creating the Gemini Enterprise app %s", engine_id)
    if created():
        return
    url = console_url("gemini-enterprise/start", api.project)
    step = NOT_ENABLED.format(project=api.project, url=url)
    retry = "The app still cannot be created."
    if not guide_user(step, created, retry):
        raise UserStepNeeded


def ensure_own_app(api: DiscoveryEngine, engine_id: str, stores: list[str]):
    """Create our app, or reuse it and link ``stores``; return its name."""
    engine = engine_name(api, engine_id)
    try:
        app = api.get_or_none(engine)
    except GenMediaError as exc:
        if not subscription_missing(exc):
            raise
        app = None  # no Gemini Enterprise yet: create_app guides the user
    if app is None:
        create_app(api, engine_id, stores)
    else:
        logger.info("Reusing the Gemini Enterprise app %s", engine)
        add_stores(api, engine, app, stores)
    set_up_assistant(api, engine)
    return engine


def assistant_request() -> dict:
    """Our app's chat: no Google Search, the assistant model
    (GENMEDIA_ASSISTANT_MODEL) and the relay instructions."""
    return {
        "webGroundingType": "WEB_GROUNDING_TYPE_DISABLED",
        "generationConfig": {
            "defaultModelId": get_models().assistant,
            "systemInstruction": {"additionalSystemInstruction": RELAY},
        },
    }


def set_up_assistant(api: DiscoveryEngine, engine: str) -> None:
    """Set up our app's assistant; a failure only warns."""
    path = f"{engine}/assistants/default_assistant?updateMask={ASSISTANT_MASK}"
    try:
        api.call("PATCH", path, assistant_request())
    except (GenMediaError, requests.RequestException) as exc:
        logger.warning(NOT_SET, getattr(exc, "message", exc))


def connect_existing_app(api: DiscoveryEngine, app_id: str, stores):
    """Link ``stores`` to the app GEMINI_ENTERPRISE_APP_ID; return its name."""
    engine = engine_name(api, app_id)
    logger.info("Gemini Enterprise app: %s", engine)
    add_stores(api, engine, api.call("GET", engine), stores)
    return engine


def add_stores(api: DiscoveryEngine, engine: str, app: dict, stores):
    """Add the ``stores`` the app does not have yet to its ``dataStoreIds``.

    It adds and never replaces: an app linked to the data store of an older
    connector (recreated for a new URL) keeps it, because removing a data
    store is refused unless two stay linked, while going from one to two
    works."""
    current = app.get("dataStoreIds", [])
    missing = [s for s in stores if s not in current]
    older = [s for s in current if s.startswith("genmedia-studio-")]
    if not missing:
        logger.info("The app already uses data store %s", ", ".join(stores))
        return
    logger.info("Connecting data store %s to %s", ", ".join(missing), engine)
    try:
        # v1alpha, where adding a second data store was tested live. Every
        # version refuses the first one (FAILED_PRECONDITION, handled below).
        api.call(
            "PATCH",
            f"{engine}?updateMask=dataStoreIds",
            {"dataStoreIds": current + missing},
        )
    except ApiError as exc:
        refused = exc.reason == "FAILED_PRECONDITION" or (
            "remain linked" in exc.message
        )
        if not refused:
            raise
        connect_in_console(api, engine, app, missing, exc.message)
    if older:
        logger.info(
            "The app also keeps %s, an older %s data store: remove it in the "
            "console (Connected data stores) once you no longer need it.",
            ", ".join(older),
            DISPLAY_NAME,
        )


def connect_in_console(api, engine: str, app: dict, missing, message: str):
    """Guide the user to connect ``missing`` to the app in the console, and
    check with GET that the app lists them."""
    step = CONNECT_STEP.format(
        message=message,
        app=app.get("displayName") or engine.rsplit("/", 1)[-1],
        name=DISPLAY_NAME,
        stores=", ".join(missing),
        url=console_url("gemini-enterprise/", api.project),
    )

    def connected() -> bool:
        current = api.call("GET", engine).get("dataStoreIds", [])
        return all(s in current for s in missing)

    retry = f"The app does not list data store {', '.join(missing)} yet."
    if not guide_user(step, connected, retry):
        raise UserStepNeeded
