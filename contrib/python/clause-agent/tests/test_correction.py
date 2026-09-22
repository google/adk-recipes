"""Unit tests for the Correction tool and its audit trail."""

from __future__ import annotations

from clause_agent.shared_libraries import audit_log
from clause_agent.tools.correction import submit_correction


def test_submit_correction_logs_and_returns_id():
    result = submit_correction(
        field="customer_id",
        customer="Acme Corp",
        wrong_answer="not found",
        correct_value="100234",
        correct_source="2018 contract, Exhibit A",
        root_cause="search scope excluded exhibits",
        reported_by="j.kim@acme-billing",
        proposed_rule=(
            "Always include exhibits/appendices in document search scope"
            " for customer ID lookups."
        ),
    )
    assert result["status"] == "logged"
    assert result["correction_id"].startswith("COR-")

    events = audit_log.read_events()
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "correction"
    assert event["correction_id"] == result["correction_id"]
    assert event["reported_by"] == "j.kim@acme-billing"
    assert event["correct_value"] == "100234"


def test_multiple_corrections_all_logged():
    submit_correction(
        field="customer_id",
        customer="Acme Corp",
        wrong_answer="not found",
        correct_value="100234",
        correct_source="Exhibit A",
        root_cause="scope",
        reported_by="a@x.com",
    )
    submit_correction(
        field="payment_term",
        customer="Globex Inc.",
        wrong_answer="Net 30",
        correct_value="Net 45",
        correct_source="§5",
        root_cause="misread",
        reported_by="b@x.com",
    )
    events = audit_log.read_events()
    assert len(events) == 2
    assert {e["correction_id"] for e in events} == {
        events[0]["correction_id"],
        events[1]["correction_id"],
    }
    assert events[0]["correction_id"] != events[1]["correction_id"]
