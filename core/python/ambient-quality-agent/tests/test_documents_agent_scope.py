# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for `documents.agent_scope`: each agent's goal and memories.

The documents run over the autouse `jobs_store` directory, so the object names
asserted here are the ones a bucket gets.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ambient_quality_agent.tools.documents import agent_scope
from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.documents.agent_scope import (
    AgentDocumentStore,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.orchestrator import document_tools

from .conftest import StateContext

_STAGING = "acme-staging__support"
_PROD = "acme-prod__support"


@pytest.fixture
def default_agent(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make `support`, an agent in AQuA's own project, the default agent."""
    monkeypatch.setattr(
        agent_scope, "resolve_default_agent_key", lambda *_: "support"
    )
    return "support"


def test_an_agents_goal_is_kept_under_its_key(jobs_store: Any) -> None:
    goal_doc.save_goal(AgentDocumentStore(jobs_store, _STAGING), "Be brief.")

    assert jobs_store.read_text(f"agents/{_STAGING}/goal.md") == "Be brief."
    assert jobs_store.read_text("goal.md") is None
    assert goal_doc.read_goal(AgentDocumentStore(jobs_store, _PROD)) is None


def test_documents_do_not_read_as_attachments(jobs_store: Any) -> None:
    from ambient_quality_agent.tools.observed_agent_config import store

    agent = AgentDocumentStore(jobs_store, _STAGING)
    goal_doc.save_goal(agent, "Be brief.")
    memories_doc.record_memory(agent, "Prompts are in app/.", source="s")

    assert store.list_agents(jobs_store) == []


def test_an_agent_that_is_not_the_default_never_reads_the_instances_goal(
    jobs_store: Any,
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")

    assert goal_doc.read_goal(AgentDocumentStore(jobs_store, _STAGING)) is None


def test_the_default_agent_reads_the_instances_goal_until_it_saves_its_own(
    jobs_store: Any,
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)

    assert goal_doc.read_goal(agent) == "The instance's goal."

    goal_doc.save_goal(agent, "Its own goal.")

    assert goal_doc.read_goal(agent) == "Its own goal."
    assert jobs_store.read_text("goal.md") == "The instance's goal."
    assert [v.text for v in goal_doc.list_versions(agent)] == [
        "Its own goal.",
        "The instance's goal.",
    ]


def test_removing_the_default_agents_goal_does_not_bring_back_the_instances(
    jobs_store: Any,
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)

    goal_doc.save_goal(agent, "")

    assert goal_doc.read_goal(agent) is None


def test_the_default_agent_lists_and_deletes_the_instances_memories(
    jobs_store: Any,
) -> None:
    instance_memory, _ = memories_doc.record_memory(
        jobs_store, "Prompts are in app/.", source="instance"
    )
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)
    own, _ = memories_doc.record_memory(agent, "Use the v2 table.", source="s")

    assert {m.id for m in memories_doc.load_memories(agent)} == {
        instance_memory.id,
        own.id,
    }
    assert (
        memories_doc.load_memories(AgentDocumentStore(jobs_store, _PROD)) == []
    )

    assert memories_doc.delete_memory(agent, instance_memory.id)
    assert memories_doc.load_memories(jobs_store) == []
    assert [m.id for m in memories_doc.load_memories(agent)] == [own.id]


def test_recording_an_inherited_memory_again_keeps_the_stored_one(
    jobs_store: Any,
) -> None:
    instance_memory, _ = memories_doc.record_memory(
        jobs_store, "Prompts are in app/.", source="instance"
    )
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)

    memory, created = memories_doc.record_memory(
        agent, "Prompts are in app/.", source="new"
    )

    assert not created
    assert memory.source == instance_memory.source
    assert (
        jobs_store.list_names(f"agents/support/{memories_doc.MEMORIES_PREFIX}")
        == []
    )


def test_the_memory_cap_is_per_agent(
    jobs_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memories_doc, "MAX_MEMORIES", 1)
    memories_doc.record_memory(
        AgentDocumentStore(jobs_store, _STAGING), "One.", source="s"
    )

    _, created = memories_doc.record_memory(
        AgentDocumentStore(jobs_store, _PROD), "Two.", source="s"
    )

    assert created


def test_inherited_prefixes_leave_out_the_agents_documents(
    jobs_store: Any,
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)
    goal_doc.save_goal(agent, "Its own goal.")

    assert agent.list_prefixes("") == ["goals/"]


def test_an_unresolved_agent_uses_the_instances_documents(
    jobs_store: Any,
) -> None:
    assert agent_scope.resolve_document_store("") is jobs_store
    assert agent_scope.resolve_document_store(None) is jobs_store


def test_only_the_default_agent_inherits(
    jobs_store: Any, default_agent: str
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")

    default = agent_scope.resolve_document_store(default_agent)
    other = agent_scope.resolve_document_store(_STAGING)

    assert goal_doc.read_goal(default) == "The instance's goal."
    assert goal_doc.read_goal(other) is None


def test_the_goal_routes_use_the_selected_agents_documents(
    jobs_store: Any, default_agent: str
) -> None:
    selected = StateContext({effective_config.AGENT_KEY_STATE: _STAGING})

    asyncio.run(document_tools.set_goal(selected, goal="Staging's goal."))

    assert (
        jobs_store.read_text(f"agents/{_STAGING}/goal.md") == "Staging's goal."
    )
    assert (
        asyncio.run(document_tools.get_goal(selected))["goal"]
        == "Staging's goal."
    )
    assert asyncio.run(document_tools.get_goal(StateContext()))["goal"] is None


def test_the_memory_routes_use_the_selected_agents_documents(
    jobs_store: Any, default_agent: str
) -> None:
    memory, _ = memories_doc.record_memory(
        AgentDocumentStore(jobs_store, _STAGING),
        "Use the v2 table.",
        source="s",
    )
    selected = StateContext({effective_config.AGENT_KEY_STATE: _STAGING})

    listed = asyncio.run(document_tools.list_memories(selected))
    by_default = asyncio.run(document_tools.list_memories(StateContext()))

    assert [m["id"] for m in listed["memories"]] == [memory.id]
    assert listed["uri"].endswith(f"agents/{_STAGING}/memories/")
    assert by_default["memories"] == []
    assert asyncio.run(
        document_tools.delete_memory(selected, memory_id=memory.id)
    ) == {"deleted": True}


def test_the_default_agents_investigation_reads_the_instances_goal(
    jobs_store: Any, default_agent: str
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")

    loaded = goal_doc.load_investigation_goal(
        agent_scope.resolve_document_store(default_agent)
    )

    assert loaded.text == "The instance's goal."
    assert loaded.source == "saved"


def test_documents_follow_a_change_of_default_agent(
    jobs_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal_doc.save_goal(jobs_store, "The instance's goal.")

    monkeypatch.setattr(agent_scope, "resolve_default_agent_key", lambda: _PROD)

    assert goal_doc.read_goal(agent_scope.resolve_document_store(_PROD)) == (
        "The instance's goal."
    )
    assert (
        goal_doc.read_goal(agent_scope.resolve_document_store("support"))
        is None
    )


def test_inherited_names_leave_out_every_agents_documents(
    jobs_store: Any,
) -> None:
    goal_doc.save_goal(AgentDocumentStore(jobs_store, _PROD), "Prod's goal.")
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)

    assert not any(n.startswith("agents/") for n in agent.list_names(""))


@pytest.fixture
def unreadable_attachments(
    jobs_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attachment exists, but reading the attachments fails."""
    from ambient_quality_agent.tools.observed_agent_config import (
        store as agent_store,
    )

    def fail(store: Any) -> str | None:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(agent_store, "resolve_default", fail)


def test_an_unreadable_default_agent_is_not_read_as_no_agent(
    jobs_store: Any, unreadable_attachments: None
) -> None:
    """Read as no agent, a save would land in the instance's goal.md, which
    the default agent's own goal shadows."""
    saved = asyncio.run(document_tools.set_goal(StateContext(), goal="Lost."))
    goal = asyncio.run(document_tools.get_goal(StateContext()))

    assert "bucket unreachable" in saved["error"]
    assert goal["available"] is False
    assert jobs_store.read_text("goal.md") is None
    with pytest.raises(RuntimeError):
        agent_scope.inherits_instance_documents(_PROD)


def test_a_run_whose_default_agent_is_unreadable_reviews_without_a_goal(
    jobs_store: Any, unreadable_attachments: None
) -> None:
    from unittest import mock

    from ambient_quality_agent.core.nodes import _common

    loaded = _common._load_default_goal(
        mock.MagicMock(state={"agent_key": _PROD})
    )

    assert loaded.source == "unreadable"


def test_the_default_agent_never_reaches_another_agents_objects(
    jobs_store: Any,
) -> None:
    """The root's `agents/` is every agent's, never the instance's, so no name
    under it is read, written or deleted through the root."""
    jobs_store.write_text(f"agents/{_PROD}.json", "{}")
    goal_doc.save_goal(AgentDocumentStore(jobs_store, _PROD), "Prod's goal.")
    agent = AgentDocumentStore(jobs_store, "support", inherit=True)

    assert agent.read_text(f"agents/{_PROD}.json") is None
    assert agent.read_bytes(f"agents/{_PROD}/goal.md") is None
    assert agent.write_text(f"agents/{_PROD}.json", "x", overwrite=False)
    assert not agent.delete(f"agents/{_PROD}/goal.md")
    assert agent.list_prefixes("agents/") == []
    # Only the copy the agent wrote under its own prefix, never the root's.
    assert agent.list_names("agents/") == [f"agents/{_PROD}.json"]
    assert jobs_store.read_text(f"agents/{_PROD}.json") == "{}"
    assert jobs_store.read_text(f"agents/{_PROD}/goal.md") == "Prod's goal."
