"""Integration test reproducing the PRD's "Simulated Session" trace.

This drives the REAL agent tree (root_agent -> hierarchy_resolver /
clause_extractor) through a `ScriptedModel` (see `tests/fakes.py`) so only
the LLM's *decisions* (which tool to call, with what args, and what to say)
are scripted -- every tool actually executes for real: `search_documents`
hits the real corpus, `memory_bank_search`/`memory_bank_create` hit the real
(tmp-path-isolated) local backend, `submit_correction` writes to the real
audit log, and `request_legal_review`'s pending/resume cycle goes through
the real `Runner` resumption mechanism (verified directly against
`google-adk`'s source: `Runner._resolve_invocation_id` matches an incoming
`function_response`'s id against a prior `function_call` event in session
history -- this is a generic Runner mechanism, not something reimplemented
here).

Covers PRD TC1 (precedence conflict + Legal loop) and TC3 (not-found +
correction + cross-customer rule generalization), matching Act 1 / Act 2 of
the PRD's trace turn-by-turn.
"""

from __future__ import annotations

from google.adk.runners import InMemoryRunner
from google.genai import types

from clause_agent.agent import build_root_agent
from clause_agent.tools.legal_review import resolve_legal_review
from tests.fakes import ScriptedModel, call, text, text_and_call

APP_NAME = "clause_agent_trace_test"


def _user_message(text_value: str) -> types.Content:
    return types.Content(
        role="user", parts=[types.Part.from_text(text=text_value)]
    )


def _function_response_message(
    call_id: str, name: str, response: dict
) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id, name=name, response=response
                )
            )
        ],
    )


def _find_function_call_id(events, tool_name: str) -> str:
    for event in events:
        if not event.content:
            continue
        for part in event.content.parts:
            if part.function_call and part.function_call.name == tool_name:
                return part.function_call.id
    raise AssertionError(f"No function_call to {tool_name!r} found in events.")


def _find_function_response(events, tool_name: str) -> dict:
    for event in events:
        if not event.content:
            continue
        for part in event.content.parts:
            if (
                part.function_response
                and part.function_response.name == tool_name
            ):
                return part.function_response.response
    raise AssertionError(f"No function_response for {tool_name!r} found.")


def _final_text(events) -> str:
    for event in reversed(events):
        if event.content and event.content.role == "model":
            texts = [p.text for p in event.content.parts if p.text]
            if texts:
                return " ".join(texts)
    raise AssertionError("No final model text found in events.")


async def test_prd_simulated_session_act1_and_act2(request):
    app_name = f"{APP_NAME}_{request.node.name}"

    model = ScriptedModel(
        responses=[
            # --- Turn 1: Billing Analyst asks about Acme Corp's payment term ---
            text_and_call(
                "This requires resolving contract precedence before I can"
                " answer. Delegating to hierarchy_resolver.",
                "transfer_to_agent",
                agent_name="hierarchy_resolver",
            ),
            call(
                "memory_bank_search",
                scope={"customer": "Acme Corp", "clause": "payment_term"},
            ),
            call(
                "search_documents", customer="Acme Corp", query="payment term"
            ),
            text_and_call(
                "Conflict found between the 2018 MSA and the 2025 Renewal."
                " Escalating to Legal instead of guessing.",
                "request_legal_review",
                customer="Acme Corp",
                question=(
                    "Does the 2025 renewal's 60-day payment term override"
                    " the 2018 contract's 30-day term for Product X?"
                ),
                proposed_answer=(
                    "Yes -- 60 days, per 2025 Renewal §4.2 and the override"
                    " clause in §9"
                ),
                sources=[
                    "2018_MSA_AcmeCorp.pdf#§7",
                    "2025_Renewal_AcmeCorp_ProductX.pdf#§4.2",
                    "2025_Renewal_AcmeCorp_ProductX.pdf#§9",
                ],
                confidence=0.78,
            ),
            text(
                "I found a conflict between the 2018 and 2025 contracts. "
                "I've sent it to Legal to confirm -- I'll let you know as "
                "soon as it's approved."
            ),
            # --- Turn 2 (resume after Legal approval) ---
            call(
                "memory_bank_create",
                scope={
                    "customer": "Acme Corp",
                    "product": "ProductX",
                    "clause": "payment_term",
                },
                fact="60 days to pay, per 2025 Renewal §4.2, overriding 2018 §7",
                citation="2025 Renewal §4.2, §9",
                approved_by="l.martinez@legal.acme",
                approved_at="2026-08-04T14:32:00Z",
            ),
            # hierarchy_resolver's job is done -- hand back to root_agent,
            # which delivers the final answer to the user (matches the PRD
            # trace's "--- root_agent -> user ---" attribution).
            call("transfer_to_agent", agent_name="root_agent"),
            text(
                "Update: Legal approved it. Acme Corp has 60 days to pay "
                "this invoice (2025 Renewal §4.2, overriding the older "
                "2018 term). Approved by L. Martinez (Legal)."
            ),
            # --- Turn 3: a different analyst asks for Acme Corp's customer ID ---
            # root_agent is active again (hierarchy_resolver handed back
            # above), so it re-routes -- this matches the PRD trace showing
            # root_agent issuing transfer_to_agent for every new question.
            call("transfer_to_agent", agent_name="clause_extractor"),
            call(
                "memory_bank_search",
                scope={"customer": "Acme Corp", "field": "customer_id"},
            ),
            call(
                "memory_bank_search",
                scope={
                    "rule_type": "document_search_scope",
                    "field": "customer_id",
                },
            ),
            call("search_documents", customer="Acme Corp", query="customer ID"),
            call("transfer_to_agent", agent_name="root_agent"),
            text(
                "I couldn't find a customer ID for Acme Corp in the contract."
            ),
            # --- Turn 4: the analyst corrects the agent ---
            call("transfer_to_agent", agent_name="clause_extractor"),
            call(
                "submit_correction",
                field="customer_id",
                customer="Acme Corp",
                wrong_answer="not found",
                correct_value="100234",
                correct_source="2018 contract, Exhibit A",
                root_cause="search scope excluded exhibits",
                reported_by="j.kim@acme-billing",
                proposed_rule=(
                    "Always search exhibits/appendices for customer ID lookups."
                ),
            ),
            call(
                "memory_bank_create",
                scope={"customer": "Acme Corp", "field": "customer_id"},
                fact="100234",
                citation="2018 contract, Exhibit A",
            ),
            call(
                "memory_bank_create",
                scope={
                    "rule_type": "document_search_scope",
                    "field": "customer_id",
                },
                fact=(
                    "Always search exhibits/appendices for customer ID lookups."
                ),
            ),
            call("transfer_to_agent", agent_name="root_agent"),
            text(
                "Thanks, corrected -- Acme Corp's customer ID is 100234 "
                "(2018 contract, Exhibit A). I've also saved this as a "
                "standing rule for future lookups."
            ),
            # --- Turn 5: a different customer benefits from the rule ---
            call("transfer_to_agent", agent_name="clause_extractor"),
            call(
                "memory_bank_search",
                scope={"customer": "Globex Inc.", "field": "customer_id"},
            ),
            call(
                "memory_bank_search",
                scope={
                    "rule_type": "document_search_scope",
                    "field": "customer_id",
                },
            ),
            call(
                "search_documents",
                customer="Globex",
                query="customer ID",
                scope=["body", "exhibit"],
            ),
            call("transfer_to_agent", agent_name="root_agent"),
            text(
                "Globex Inc.'s customer ID is 220987 (2022 contract, Exhibit B)."
            ),
        ]
    )

    agent = build_root_agent(model=model)
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id="ar.chen"
    )

    # ---------------- Turn 1 ----------------
    turn1_events = [
        e
        async for e in runner.run_async(
            user_id="ar.chen",
            session_id=session.id,
            new_message=_user_message(
                "SAP is blocking Acme Corp's Q3 invoice -- how many days"
                " does Acme Corp have to pay this invoice?"
            ),
        )
    ]
    legal_review_response = _find_function_response(
        turn1_events, "request_legal_review"
    )
    assert legal_review_response["status"] == "pending"
    task_id = legal_review_response["task_id"]
    assert task_id.startswith("LR-")
    assert "waiting" in _final_text(
        turn1_events
    ).lower() or "sent it to legal" in (_final_text(turn1_events).lower())

    fc_id = _find_function_call_id(turn1_events, "request_legal_review")

    # ---------------- Legal Reviewer resolves out-of-band ----------------
    resolved = resolve_legal_review(
        task_id=task_id,
        decision="approved",
        approver="l.martinez@legal.acme",
        comment="Confirmed -- the 2025 override clause is unambiguous.",
    )
    assert resolved.status == "approved"

    # ---------------- Turn 2 (resume) ----------------
    turn2_events = [
        e
        async for e in runner.run_async(
            user_id="ar.chen",
            session_id=session.id,
            new_message=_function_response_message(
                fc_id,
                "request_legal_review",
                {
                    "status": "approved",
                    "approver": resolved.approver,
                    "comment": resolved.comment,
                    "final_answer": resolved.final_answer,
                },
            ),
        )
    ]
    memory_create_response = _find_function_response(
        turn2_events, "memory_bank_create"
    )
    assert memory_create_response["status"] == "written"
    assert "60" in _final_text(turn2_events)

    # The approved ruling must now be retrievable from Memory Bank.
    from clause_agent.tools.memory_bank import memory_bank_search

    ruling = memory_bank_search(
        scope={
            "customer": "Acme Corp",
            "product": "ProductX",
            "clause": "payment_term",
        }
    )
    assert len(ruling["memories"]) == 1
    assert ruling["memories"][0]["approved_by"] == "l.martinez@legal.acme"

    # ---------------- Turn 3: customer ID not found ----------------
    turn3_events = [
        e
        async for e in runner.run_async(
            user_id="ar.chen",
            session_id=session.id,
            new_message=_user_message(
                "What's Acme Corp's customer ID? I need it to create the"
                " invoice in SAP."
            ),
        )
    ]
    search_response = _find_function_response(turn3_events, "search_documents")
    assert search_response["hits"] == []
    assert "not found" in _final_text(
        turn3_events
    ).lower() or "couldn't find" in (_final_text(turn3_events).lower())

    # ---------------- Turn 4: correction ----------------
    turn4_events = [
        e
        async for e in runner.run_async(
            user_id="ar.chen",
            session_id=session.id,
            new_message=_user_message(
                "That's wrong. It's 100234 -- it's in Exhibit A of the 2018"
                " contract, which is an attachment, not the main body. You"
                " only searched the body. You need to check"
                " attachments/exhibits as well, every time."
            ),
        )
    ]
    correction_response = _find_function_response(
        turn4_events, "submit_correction"
    )
    assert correction_response["status"] == "logged"
    assert "100234" in _final_text(turn4_events)

    from clause_agent.shared_libraries import audit_log

    events_logged = audit_log.read_events()
    assert any(e["event_type"] == "correction" for e in events_logged)

    # Both the narrow fact and the broad rule must now be on file.
    customer_id_memory = memory_bank_search(
        scope={"customer": "Acme Corp", "field": "customer_id"}
    )
    assert customer_id_memory["memories"][0]["fact"] == "100234"

    rule_memory = memory_bank_search(
        scope={"rule_type": "document_search_scope", "field": "customer_id"}
    )
    assert len(rule_memory["memories"]) == 1
    assert "exhibits" in rule_memory["memories"][0]["fact"].lower()

    # ---------------- Turn 5: a different customer benefits ----------------
    turn5_events = [
        e
        async for e in runner.run_async(
            user_id="ar.chen",
            session_id=session.id,
            new_message=_user_message("What's Globex Inc.'s customer ID?"),
        )
    ]
    globex_search_response = _find_function_response(
        turn5_events, "search_documents"
    )
    assert len(globex_search_response["hits"]) == 1
    assert "220987" in globex_search_response["hits"][0]["text"]
    assert "220987" in _final_text(turn5_events)
