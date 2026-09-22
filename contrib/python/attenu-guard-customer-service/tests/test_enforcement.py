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

"""What the recipe claims, asserted. Offline: no API key, no network."""

import pytest
from attenu_guard import AuditLog, evidence
from attenu_guard.wire import Ed25519Signer
from google.adk.apps.app import App

import demo
from app import tools
from app.agent import build_root_agent, require_guard
from app.permissions import COORDINATOR, GREEDY_REQUEST


@pytest.fixture(scope="module")
def run():
    events, root_guard, plugin = demo.run_offline()
    return events, root_guard, plugin, list(tools.EXECUTED)


def test_the_read_the_sub_agent_was_delegated_goes_through(run):
    _events, _guard, _plugin, executed = run
    assert ("get_invoice", "INV-4471") in executed


def test_the_refund_is_refused_before_the_tool_body_runs(run):
    events, _guard, _plugin, executed = run
    response = demo.tool_responses(events)["issue_refund"]

    assert response["error"] == "authority_denied"
    assert response["agent"] == "billing_agent"
    assert response["scope"] == "billing.refund"
    # The proof that this was not "run it, then report it": the tool
    # body appends to EXECUTED as its first statement.
    assert not any(name == "issue_refund" for name, _ in executed)


def test_the_sub_agent_holds_less_than_its_parent(run):
    _events, root_guard, plugin, _executed = run
    billing = plugin.guard_for("billing_agent")

    assert billing.is_narrower_than(root_guard)
    assert not root_guard.is_narrower_than(billing)
    assert "billing.refund" not in billing.authority.scopes


def test_a_request_for_more_than_the_parent_holds_is_met_down():
    granted = COORDINATOR.meet(GREEDY_REQUEST)

    assert granted.is_narrower_than(COORDINATOR)
    assert "admin.root" not in granted.scopes


def test_the_ledger_verifies_and_names_the_refusal(run):
    _events, root_guard, _plugin, _executed = run
    entries = root_guard.audit_log().entries

    ok, err = AuditLog.verify(entries)
    assert ok, err

    denials = [e for e in entries if e["event"] == "deny"]
    assert [d["scope"] for d in denials] == ["billing.refund"]


def test_a_tampered_ledger_does_not_verify(run):
    _events, root_guard, _plugin, _executed = run
    entries = [dict(e) for e in root_guard.audit_log().entries]

    for entry in entries:
        if entry["event"] == "deny":
            entry["event"] = "allow"
            break

    ok, _err = AuditLog.verify(entries)
    assert not ok


def test_the_evidence_bundle_verifies_on_its_own(run):
    _events, root_guard, _plugin, _executed = run
    signer = Ed25519Signer.generate(kid="recipe-test")

    bundle = evidence.export_bundle(root_guard.audit_log(), signer)
    report = evidence.verify_bundle(bundle, signer)

    assert report["ok"]
    assert report["checks"]["integrity"]
    assert report["checks"]["monotonicity"]
    assert report["checks"]["containment"]


def test_an_app_without_the_plugin_refuses_to_start():
    unguarded = App(
        name="unguarded",
        root_agent=build_root_agent("scripted-offline-model"),
    )

    with pytest.raises(RuntimeError, match="refusing to run unguarded"):
        require_guard(unguarded)
