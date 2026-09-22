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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Customer-service coordinator with a billing sub-agent, guarded.

Two agents, one plugin. `DelegationGuardPlugin` is registered once on the
`App`; from then on every agent in the tree carries a permission set, and
every tool call is checked against it before the tool body runs.

The coordinator can issue a refund. The billing agent it transfers to
cannot: it is delegated `billing.read` and nothing else, so its
`issue_refund` call is refused at the callback, and the function is never
entered. The refusal is appended to a hash-chained ledger that verifies
without this process, this recipe, or any network.
"""

import os
from typing import Any

from attenu_guard import Guard
from attenu_guard.adapters.google_adk import DelegationGuardPlugin
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App

from .permissions import (
    COORDINATOR,
    DELEGATION_SCOPE,
    DELEGATIONS,
    TOOLS,
)
from .prompt import BILLING_PROMPT, COORDINATOR_PROMPT
from .tools import email_customer, get_invoice, issue_refund, lookup_order

APP_NAME = "attenu-guard-customer-service"
ROOT_AGENT_NAME = "coordinator"
BILLING_AGENT_NAME = "billing_agent"


def build_root_agent(model: Any) -> LlmAgent:
    """The agent tree. `model` is a model name or a `BaseLlm` instance,
    so the offline demo can pass a scripted model in its place."""
    billing_agent = LlmAgent(
        name=BILLING_AGENT_NAME,
        model=model,
        description="Reads invoices and explains billing questions.",
        instruction=BILLING_PROMPT,
        tools=[get_invoice, issue_refund],
    )
    return LlmAgent(
        name=ROOT_AGENT_NAME,
        model=model,
        description="Handles customer-service requests end to end.",
        instruction=COORDINATOR_PROMPT,
        tools=[lookup_order, email_customer],
        sub_agents=[billing_agent],
    )


def build_plugin(root_guard: Guard) -> DelegationGuardPlugin:
    """One plugin covers the whole tree."""
    return DelegationGuardPlugin(
        root_guard,
        root_agent_name=ROOT_AGENT_NAME,
        delegations=DELEGATIONS,
        tools=TOOLS,
        delegation_scope=DELEGATION_SCOPE,
    )


def build_app(
    model: Any,
    *,
    task: str = "customer service",
    audit_path: str | None = None,
) -> tuple[App, Guard, DelegationGuardPlugin]:
    """An `App` with the guard attached, plus the root `Guard` and the
    plugin. You keep the root Guard: it owns the ledger, the delegation
    graph, and `revoke()`."""
    root_guard = Guard.issue(
        ROOT_AGENT_NAME, COORDINATOR, task=task, audit_path=audit_path
    )
    plugin = build_plugin(root_guard)
    application = App(
        name=APP_NAME,
        root_agent=build_root_agent(model),
        plugins=[plugin],
    )
    require_guard(application)
    return application, root_guard, plugin


def require_guard(application: App) -> None:
    """Refuse to run an App that lost its guard.

    Exported for callers that assemble their own `App` rather than using
    `build_app` — call it once after construction. Inside `build_app` it
    cannot fail today, deliberately: it is a tripwire so a future edit
    that drops the plugin turns into a startup failure instead of a
    silent downgrade.
    """
    plugins = getattr(application, "plugins", None) or []
    if not any(isinstance(p, DelegationGuardPlugin) for p in plugins):
        raise RuntimeError(
            "DelegationGuardPlugin is not attached to this App — "
            "refusing to run unguarded"
        )


# The module-level objects the ADK CLI looks for. `adk run` and `adk web`
# check for `app` first and fall back to `root_agent`, so exposing the App
# is what carries the plugin into a CLI-driven run.
#
# They are built lazily (PEP 562): only a CLI-driven run touches these
# names, and that run requires MODEL_NAME (see `.env.example`) — there is
# deliberately no in-code default. `demo.py` and the tests never read
# them; they call `build_app()` with their own model, so importing this
# module stays side-effect-free for them.
#
# One caveat worth knowing: a CLI run builds once, on first access, so
# every session `adk web` serves shares that root Guard and its ledger,
# and the time-to-live starts counting then. That is fine for trying the
# recipe out. For anything where one caller's decisions should not be
# another's evidence, call `build_app()` per run.
_cli_singletons: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name not in ("app", "root_agent", "root_guard", "guard_plugin"):
        raise AttributeError(name)
    if not _cli_singletons:
        model = os.getenv("MODEL_NAME")
        if not model:
            raise RuntimeError(
                "MODEL_NAME is not set - a CLI-driven run (`adk run app`, "
                "`adk web`) needs it; see .env.example. The offline demo "
                "(`python demo.py`) does not use it."
            )
        application, guard, plugin = build_app(model)
        _cli_singletons.update(
            app=application,
            root_agent=application.root_agent,
            root_guard=guard,
            guard_plugin=plugin,
        )
    return _cli_singletons[name]
