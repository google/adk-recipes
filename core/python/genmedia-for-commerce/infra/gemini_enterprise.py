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

"""Register the GenMedia Studio MCP App in a Gemini Enterprise app.

``uv run python infra/gemini_enterprise.py [--dry-run]``, run by
``make register-gemini-enterprise``. It does everything by itself and can run
again; for the console-only steps it prints what to do, waits for Enter and
checks the result (when stdin is a terminal; otherwise it prints the step).
``--allow-mcp-connectors`` (``make allow-mcp-connectors``) only allows custom
MCP connectors in the app's project (see step 2).

1. Finds the Cloud Run service (PROJECT_ID, DEFAULT_REGION and SERVICE_NAME
   from config.env; the Studio is at ``<URL>/mcp``) and lets the Discovery
   Engine service agent invoke it.
2. Creates a custom MCP data connector unless one already serves the same URL
   (when the organisation policy blocks it, asking in a terminal to allow
   custom MCP connectors in the project, then waiting until that applies)
   and activates its actions.
3. Creates or reuses its own Gemini Enterprise app, "GenMedia Studio", with
   the connector's data store linked (waiting for Gemini Enterprise to be
   enabled when it is not), or connects the data store to the app
   GEMINI_ENTERPRISE_APP_ID (a full engine resource name or an id in
   GEMINI_ENTERPRISE_LOCATION, default global).
4. Enables the Studio's action and waits until "Reload custom actions" has run.

The connector API is v1alpha and only documented by example, so any other
failure prints the error and the equivalent console steps.
"""

import argparse
import logging
import os
import subprocess
import sys

import requests

from genmedia4commerce.core.errors import ConfigError, GenMediaError
from infra.gemini_enterprise_api import (
    RERUN,
    DiscoveryEngine,
    UserStepNeeded,
    console_url,
    gcloud,
)
from infra.gemini_enterprise_app import connect_existing_app, ensure_own_app
from infra.gemini_enterprise_policy import (
    POLICY,
    allow_mcp_connectors_in,
    blocked_steps,
)
from infra.gemini_enterprise_steps import (
    DISPLAY_NAME,
    STUDIO_ACTIONS,
    activate,
    create_collection,
    data_store_ids,
    enable_actions,
    find_collection,
    wait_for_actions,
)

logger = logging.getLogger(__name__)

MANUAL_STEPS = """
Do it in the console instead (Google Cloud console > Gemini Enterprise):
  1. Data stores > Create data store > Custom MCP Server.
  2. URL {mcp_url}, No authentication (Cloud Run IAM sends the ID token for .run.app URLs) > Create.
  3. Open the data store > Actions > Reload custom actions, and enable them.
  4. Apps > Create app (or open yours) > Connected data stores: add the new data store.
Custom MCP connectors may be blocked by an organisation policy, and you need the Discovery Engine Editor role (roles/discoveryengine.editor)."""

DONE = """
{status}
  App: {app} ({engine})
  Console: {apps} > Apps > {app}
  Users need a Gemini Enterprise licence assigned to use the app (Manage users): {licences}
  To open the Studio: open the app's web app (Apps > {app} > Integration > the link to your web app), turn on {name} under Connectors in the chat and ask "Open the GenMedia Studio"."""


def grant_invoker(api: DiscoveryEngine, run_args: list[str], dry_run: bool):
    """Let the app project's Discovery Engine service agent invoke Studio."""
    number = gcloud(
        "projects", "describe", api.project, "--format=value(projectNumber)"
    )
    agent = f"service-{number}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
    commands = [
        [
            *("beta", "services", "identity", "create"),
            "--service=discoveryengine.googleapis.com",
            f"--project={api.project}",
        ],
        [
            *("run", "services", "add-iam-policy-binding", *run_args),
            f"--member=serviceAccount:{agent}",
            "--role=roles/run.invoker",
        ],
    ]
    logger.info(
        "Granting roles/run.invoker on the service to %s (every Gemini "
        "Enterprise app and connector of project %s acts as it: prefer a "
        "project dedicated to GenMedia for Commerce)",
        agent,
        api.project,
    )
    for command in commands:
        if dry_run:
            logger.info("[dry run] gcloud %s", " ".join(command))
        else:
            gcloud(*command, "--quiet")


def studio_url() -> tuple[list[str], str]:
    """``gcloud run services`` arguments of the service, and the Studio URL."""
    project = os.environ.get("PROJECT_ID", "")
    if not project:
        raise ConfigError("PROJECT_ID is not set in config.env")
    run_args = [
        os.environ.get("SERVICE_NAME") or "genmedia-for-commerce",
        f"--region={os.environ.get('DEFAULT_REGION') or 'us-central1'}",
        f"--project={project}",
    ]
    url = gcloud(
        "run", "services", "describe", *run_args, "--format=value(status.url)"
    )
    if not url:
        raise ConfigError("The service is not deployed: run make deploy")
    return run_args, f"{url}/mcp"


def warn_if_iap() -> None:
    """Warn that a Studio behind IAP cannot be called by Gemini Enterprise."""
    if os.environ.get("USE_IAP", "").strip().lower() != "true":
        return
    logger.warning(
        "WARNING: USE_IAP=true in config.env. Gemini Enterprise calls the "
        "Studio with its service agent's ID token, which passes Cloud Run "
        "IAM (roles/run.invoker, granted below) but not IAP, so every call "
        "will be rejected. Deploy with USE_IAP=false to use the Studio from "
        "Gemini Enterprise."
    )


def gemini_enterprise_app() -> str:
    """GEMINI_ENTERPRISE_APP_ID: a full engine name, an id or empty."""
    return os.environ.get("GEMINI_ENTERPRISE_APP_ID") or os.environ.get(
        "ID", ""
    )


def app_project(app_id: str) -> str:
    """The app's project: the one of a full engine name, else PROJECT_ID."""
    parts = app_id.split("/")  # projects/<p>/locations/<loc>/... or an id
    return parts[1] if len(parts) > 3 else os.environ.get("PROJECT_ID", "")


def client(app_id: str, dry_run: bool):
    """The REST client for the app's project and location."""
    parts = app_id.split("/")
    location = parts[3] if len(parts) > 3 else None
    location = (
        location or os.environ.get("GEMINI_ENTERPRISE_LOCATION") or "global"
    )
    token = gcloud("auth", "print-access-token")
    return DiscoveryEngine(app_project(app_id), location, token, dry_run)


def register(run_args: list[str], mcp_url: str, dry_run: bool) -> None:
    warn_if_iap()
    app_id = gemini_enterprise_app()
    api = client(app_id, dry_run)
    grant_invoker(api, run_args, dry_run)
    reused = find_collection(api, mcp_url)
    if reused:
        logger.info("Reusing data connector %s (same URL)", reused)
    cid = reused or create_collection(api, mcp_url)
    activate(api, cid, mcp_url)
    # A dry run creates no connector: name the data store it would have.
    if dry_run and not reused:
        stores = [f"{cid}_mcp_data"]
    else:
        stores = data_store_ids(api, cid)
    if app_id:
        engine = connect_existing_app(api, app_id, stores)
    else:  # our own app, with a stable id so a second run reuses it
        engine = ensure_own_app(api, f"{run_args[0]}-studio", stores)
    enable_actions(api, cid)
    if dry_run:
        logger.info(
            "[dry run] would check that Gemini Enterprise has loaded %s",
            ", ".join(STUDIO_ACTIONS),
        )
        return
    finish(api, engine, app_id, wait_for_actions(api, cid))


def finish(api: DiscoveryEngine, engine: str, app_id: str, loaded: bool):
    """Say where the app is and how to use the Studio in it."""
    status = (
        f"Done: {DISPLAY_NAME} is registered in Gemini Enterprise."
        if loaded
        else "Registered, except for the console step above."
    )
    logger.info(
        DONE.format(
            status=status,
            app=engine.rsplit("/", 1)[-1] if app_id else DISPLAY_NAME,
            engine=engine,
            apps=console_url("gemini-enterprise/", api.project),
            licences=console_url("gemini-enterprise/user-license", api.project),
            name=DISPLAY_NAME,
        )
    )


def allow_mcp_connectors() -> int:
    """``make allow-mcp-connectors``: allow custom MCP connectors in the
    app's project only."""
    project = app_project(gemini_enterprise_app())
    if not project:
        logger.error("Error: PROJECT_ID is not set in config.env")
        return 1
    return allow_mcp_connectors_in(project)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    parser.add_argument(
        "--allow-mcp-connectors",
        action="store_true",
        help="only allow custom MCP connectors in the project",
    )
    args = parser.parse_args()
    if args.allow_mcp_connectors:
        return allow_mcp_connectors()
    mcp_url = "https://<Cloud Run service URL>/mcp"
    try:
        run_args, mcp_url = studio_url()
        register(run_args, mcp_url, args.dry_run)
    except UserStepNeeded:
        return 1  # the step and "run again" have been printed
    except KeyboardInterrupt:
        logger.error("\nStopped. Run make register-gemini-enterprise again.")
        return 130
    except (GenMediaError, requests.RequestException) as exc:
        logger.error("Error: %s", exc)
        if POLICY in str(exc):
            logger.error(blocked_steps(app_project(gemini_enterprise_app())))
            logger.error(RERUN)
            return 1
    except subprocess.CalledProcessError as exc:
        logger.error("Error: gcloud %s\n%s", " ".join(exc.cmd[1:]), exc.stderr)
    else:
        return 0
    logger.error(MANUAL_STEPS.format(mcp_url=mcp_url))
    return 1


if __name__ == "__main__":
    sys.exit(main())
