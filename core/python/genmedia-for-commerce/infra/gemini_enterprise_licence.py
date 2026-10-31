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


"""Gemini Enterprise licences for the Studio's app.

Nobody can open a Gemini Enterprise app without a licence. Creating the
first app in the console also starts a 30-day free trial; creating it with
the API, as make register-gemini-enterprise does, does not. When the app's
location has no licence configuration, the registration offers the same
free trial (only in a terminal; Enter, y or yes is a yes): it creates the
trial's licence configuration, makes it the user store's default with a
licence assigned at each user's first sign-in (as the console does), and
assigns one to the deployer now. The user store only exists once an app
does, so this runs after the app is created.
"""

import logging
import sys

from infra.gemini_enterprise_api import DiscoveryEngine, console_url

logger = logging.getLogger(__name__)

TRIAL_ID = "free_trial_gemini"
TRIAL = {  # what the console creates with the first app
    "licenseCount": "50",
    "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
    "subscriptionTerm": "SUBSCRIPTION_TERM_ONE_MONTH",
    "freeTrial": True,
    "geminiBundle": True,
}
USER_STORE = "userStores/default_user_store"
QUESTION = (
    "\nProject {project} has no Gemini Enterprise licence, so nobody can open "
    "the app yet. Gemini Enterprise offers a 30-day free trial with 50 "
    "licences, the one the console starts with the first app."
)
ASK = "Start the free trial and assign a licence to {user}? [Y/n] "
STARTED = (
    "Free trial started (50 licences for 30 days, {start} to {end}); {user} "
    "has one, and every other user gets one at first sign-in."
)
NO_LICENCE = (
    "Project {project} has no Gemini Enterprise licence, so nobody can open "
    "the app yet. Start the 30-day free trial or add a subscription, then "
    "assign licences (Manage users): {url}"
)


def _date(value: dict) -> str:
    return "{year}-{month:02d}-{day:02d}".format(**value) if value else "?"


def _ask(project: str, user: str) -> bool:
    """Offer the free trial; Enter, y or yes is a yes."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    logger.warning(QUESTION.format(project=project))
    try:
        answer = input(ASK.format(user=user or "you"))
    except EOFError:
        return False
    return answer.strip().lower() in ("", "y", "yes")


def ensure_licence(api: DiscoveryEngine, user: str) -> None:
    """Offer the free trial when the app's location has no licence
    configuration; otherwise leave the licences as they are."""
    if api.call("GET", f"{api.parent}/licenseConfigs", version="v1").get(
        "licenseConfigs"
    ):
        return
    if not _ask(api.project, user):
        url = console_url("gemini-enterprise/user-license", api.project)
        logger.warning(NO_LICENCE.format(project=api.project, url=url))
        return
    config = api.call(
        "POST",
        f"{api.parent}/licenseConfigs?licenseConfigId={TRIAL_ID}",
        TRIAL,
        version="v1",
    )
    name = config["name"]
    api.call(
        "PATCH",
        f"{api.parent}/{USER_STORE}"
        "?updateMask=defaultLicenseConfig,enableLicenseAutoRegister",
        {"defaultLicenseConfig": name, "enableLicenseAutoRegister": True},
        version="v1",
    )
    if user:
        licence = {"userPrincipal": user, "licenseConfig": name}
        paths = ["userPrincipal", "licenseConfig"]
        api.wait(
            api.call(
                "POST",
                f"{api.parent}/{USER_STORE}:batchUpdateUserLicenses",
                {
                    "inlineSource": {
                        "userLicenses": [licence],
                        "updateMask": {"paths": paths},
                    }
                },
                version="v1",
            )
        )
    logger.info(
        STARTED.format(
            start=_date(config.get("startDate", {})),
            end=_date(config.get("endDate", {})),
            user=user or "nobody yet",
        )
    )
