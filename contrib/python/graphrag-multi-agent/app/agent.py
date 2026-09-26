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

"""GraphRAG multi-agent system over a Neo4j knowledge graph.

A root orchestrator delegates investment-research questions to three
specialist sub-agents that all read from the same Neo4j graph:

- ``graph_database_agent`` — a general-purpose Text-to-Cypher agent. It
  reads the live schema, generates read-only Cypher, and self-corrects on
  errors. The fallback for anything the specialists cannot answer.
- ``investor_research_agent`` — a focused agent with a single hand-written
  tool for looking up a company's investors.
- ``investment_research_agent`` — loads pre-validated, expert-authored
  queries from an MCP Toolbox server when one is configured, and otherwise
  falls back to the schema + Cypher tools.

The recipe runs read-only against Neo4j's public companies demo database
(``neo4j+s://demo.neo4jlabs.com``), so no database provisioning is needed.

Import safety: the Neo4j driver is created lazily on the first tool call,
and the MCP Toolbox is contacted only when ``MCP_TOOLBOX_URL`` is set to a
real endpoint. Importing this module therefore performs no network I/O,
which is what keeps ``tests/test_runnability.py`` fast and offline.
"""

import logging
import os
import re
from typing import Any

from google.adk.agents import Agent
from neo4j import GraphDatabase, RoutingControl
from neo4j.exceptions import Neo4jError
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Duration, Time

logger = logging.getLogger(__name__)

# Model id is read from the environment (declared in .env.example), never
# hardcoded. .env is loaded by app/__init__.py before this module is imported.
MODEL = os.getenv("MODEL_NAME")

# A fast pre-check that rejects obvious mutating statements (and LOAD CSV)
# with a friendly message before they reach the database. This is only
# defense-in-depth: read-only execution is ENFORCED at the database through
# routing_=RoutingControl.READ (see execute_read_query). A keyword filter
# alone cannot make LLM-generated Cypher safe — for untrusted input, also
# connect with a database user that has read-only privileges (see the
# README "Security" note).
_WRITE_QUERY_RE = re.compile(
    r"\b(MERGE|CREATE|SET|DELETE|REMOVE|ADD|LOAD)\b", re.IGNORECASE
)

# Neo4j driver timeouts (seconds). Bounding every outbound call keeps a slow
# or dead server from hanging the agent indefinitely.
_CONNECTION_TIMEOUT_S = 15
_CONNECTION_ACQUISITION_TIMEOUT_S = 30
_MAX_CONNECTION_LIFETIME_S = 3600


def serialize_neo4j_value(value: Any) -> Any:
    """Convert Neo4j temporal types into JSON-serializable Python values.

    Neo4j returns its own ``DateTime``/``Date``/``Time``/``Duration``
    objects, which are not JSON-serializable and would break the tool
    responses handed back to the model. Containers are handled recursively.
    """
    if isinstance(value, (DateTime, Date, Time)):
        return value.isoformat()
    if isinstance(value, Duration):
        return str(value)
    if isinstance(value, Node):
        return {
            "_labels": sorted(value.labels),
            "_element_id": value.element_id,
            **{k: serialize_neo4j_value(v) for k, v in dict(value).items()},
        }
    if isinstance(value, Relationship):
        return {
            "_type": value.type,
            "_element_id": value.element_id,
            **{k: serialize_neo4j_value(v) for k, v in dict(value).items()},
        }
    if isinstance(value, Path):
        return {
            "nodes": [serialize_neo4j_value(n) for n in value.nodes],
            "relationships": [
                serialize_neo4j_value(r) for r in value.relationships
            ],
        }
    if isinstance(value, dict):
        return {k: serialize_neo4j_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_neo4j_value(item) for item in value]
    return value


class Neo4jDatabase:
    """A thin, read-only wrapper around a Neo4j driver."""

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str | None = None,
    ) -> None:
        # Bound every outbound call: cap connection setup, pool-acquisition
        # wait, and connection lifetime so a slow or dead server cannot hang
        # the agent indefinitely.
        driver = GraphDatabase.driver(
            uri,
            auth=(username, password),
            connection_timeout=_CONNECTION_TIMEOUT_S,
            connection_acquisition_timeout=_CONNECTION_ACQUISITION_TIMEOUT_S,
            max_connection_lifetime=_MAX_CONNECTION_LIFETIME_S,
        )
        # Close the driver if the initial handshake fails, so a bad
        # connection does not leak the pool and its background threads.
        try:
            driver.verify_connectivity()
        except Exception:
            driver.close()
            raise
        self.driver = driver
        self.database = database

    def is_write_query(self, query: str) -> bool:
        """Return True if the statement would modify the graph."""
        return _WRITE_QUERY_RE.search(query) is not None

    def execute_read_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a read-only Cypher query and return rows as plain dicts."""
        if self.is_write_query(query):
            raise ValueError(
                "Write queries are not supported by this read-only agent."
            )
        # routing_=READ enforces read-only at the database: the server
        # rejects any write in this transaction, regardless of the query text.
        result = self.driver.execute_query(
            query,
            params or {},
            database_=self.database,
            routing_=RoutingControl.READ,
        )
        return [serialize_neo4j_value(dict(r)) for r in result.records]


_db: Neo4jDatabase | None = None


def _get_db() -> Neo4jDatabase:
    """Return a lazily-initialised, process-wide Neo4j connection.

    Connecting on first use (rather than at import time) is what lets
    ``import app.agent`` succeed with no live database or credentials.
    """
    global _db
    if _db is None:
        _db = Neo4jDatabase(
            os.getenv("NEO4J_URI"),
            os.getenv("NEO4J_USERNAME"),
            os.getenv("NEO4J_PASSWORD"),
            os.getenv("NEO4J_DATABASE"),
        )
    return _db


def _error_result(exc: Exception) -> list[dict[str, Any]]:
    """Turn an exception into an agent-facing error row without leaking
    connection details.

    Query-level Neo4j errors (syntax, unknown label) and our own
    ``ValueError`` carry no connection info and are returned as-is, so the
    graph_database_agent can read the message and self-correct its Cypher.
    Anything else — connection, auth, or driver failures that may contain
    hostnames, ports or credentials — is logged in full server-side and
    replaced with a generic message.
    """
    if isinstance(exc, (Neo4jError, ValueError)):
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None) or str(exc)
        return [{"error": f"{code}: {message}" if code else message}]
    logger.exception("Neo4j operation failed")
    return [{"error": "Database operation failed; see server logs."}]


# ---------------------------------------------------------------------------
# Tools shared across the specialist agents
# ---------------------------------------------------------------------------


def get_schema() -> list[dict[str, Any]]:
    """Get the schema of the database.

    Returns node labels with their attribute types and the outgoing
    relationships between labels, for example::

        [{"label": "Person",
          "attributes": {"id": "STRING unique indexed", "name": "STRING"},
          "relationships": {"HAS_PARENT": "Person"}}]

    Returns:
        A list of dictionaries describing the schema, or a single-element
        list containing an ``error`` key if the lookup failed.
    """
    try:
        return _get_db().execute_read_query(
            """
            CALL apoc.meta.data() YIELD label, property, type, other, unique,
              index, elementType
            WHERE elementType = 'node' AND NOT label STARTS WITH '_'
            WITH label,
              collect(CASE WHEN type = 'RELATIONSHIP'
                THEN [property, head(other)] END) AS relationships,
              collect(CASE WHEN type <> 'RELATIONSHIP'
                THEN [property, type
                  + CASE WHEN unique THEN " unique" ELSE "" END
                  + CASE WHEN index THEN " indexed" ELSE "" END] END)
                AS attributes
            RETURN label,
              apoc.map.fromPairs(attributes) AS attributes,
              apoc.map.fromPairs(relationships) AS relationships
            """
        )
    except Exception as exc:
        return _error_result(exc)


def execute_read_query(
    query: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Execute a read-only Neo4j Cypher query.

    Args:
        query: The Cypher statement to execute. Write statements are
            rejected.
        params: Named query parameters referenced with ``$name`` in the
            statement, or None.

    Returns:
        A list of result rows as dictionaries, or a single-element list
        containing an ``error`` key if execution failed.
    """
    try:
        return _get_db().execute_read_query(query, params or {})
    except Exception as exc:
        return _error_result(exc)


def get_investors(company: str) -> list[dict[str, Any]]:
    """Return the investors in the company with this name or id.

    Args:
        company: The exact name or id of the company to find investors for.

    Returns:
        A list of investors with their ``id``, ``name`` and ``type``
        (``Organization`` or ``Person``), or a single-element list
        containing an ``error`` key if the lookup failed.
    """
    try:
        return _get_db().execute_read_query(
            """
            MATCH (o:Organization)<-[:HAS_INVESTOR]-(i)
            WHERE (o.name = $company OR o.id = $company)
              AND NOT exists { (o)<-[:HAS_SUBSIDIARY]-() }
            RETURN i.id AS id, i.name AS name, head(labels(i)) AS type
            """,
            {"company": company},
        )
    except Exception as exc:
        return _error_result(exc)


# ---------------------------------------------------------------------------
# Optional MCP Toolbox integration
# ---------------------------------------------------------------------------

MCP_TOOLBOX_URL = os.getenv("MCP_TOOLBOX_URL")


def _investment_research_tools() -> list[Any]:
    """Attach the MCP Toolbox as a lazy toolset, else a plain-tool fallback.

    The MCP Toolbox is optional: it requires the ``genai-toolbox`` binary
    running separately (see README). When ``MCP_TOOLBOX_URL`` is unset or is
    still a placeholder, this agent uses the generic schema + Cypher tools so
    the recipe always runs. When a real URL is set, the ``MCPToolset`` is
    handed to the agent as-is and ADK connects to it lazily at run time — no
    blocking network I/O at import.
    """
    fallback = [get_schema, execute_read_query]
    if not MCP_TOOLBOX_URL or MCP_TOOLBOX_URL.startswith("<"):
        return fallback
    try:
        from google.adk.tools.mcp_tool.mcp_toolset import (
            MCPToolset,
            SseConnectionParams,
        )

        toolset = MCPToolset(
            connection_params=SseConnectionParams(url=MCP_TOOLBOX_URL)
        )
        return [toolset, get_schema]
    except Exception as exc:
        logger.warning(
            "MCP Toolbox unavailable (%s); falling back to schema + Cypher "
            "tools.",
            exc,
        )
        return fallback


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

graph_database_agent = Agent(
    model=MODEL,
    name="graph_database_agent",
    description="""
    The graph_database_agent can fetch the schema of a Neo4j graph database
    and execute read queries. It generates Cypher using the schema to fulfil
    information requests and repeatedly re-creates and fixes queries that
    error or return unexpected results. When passing requests to this agent,
    give clear, specific instructions about what data to retrieve and how
    (aggregation, path expansion, sorting, filtering). Prefer more specific
    agents when they are available; use this as a fallback for structural
    questions (entity counts, aggregations) or when no other agent exposes
    the needed data.
    """,
    instruction="""
      You are a Neo4j graph database and Cypher expert. Combine the database
      schema with the user's question and repeatedly generate valid Cypher to
      execute, then answer in friendly natural language.
      When in doubt the database schema is authoritative for node labels,
      relationship types and property names; never take the user's wording at
      face value. Always validate the labels at both ends of a relationship
      against the schema.

      If a query fails or returns no data, use the error response to fix and
      re-run it up to 3 times; do not return raw errors to the user. If you
      cannot fix it, apologise and explain the issue.
      *You are prohibited* from using directional arrows (-> or <-) in graph
      patterns; always use undirected patterns like `(:Label)-[:TYPE]-(:Label)`.

      Fetch the schema first with the `get_schema` tool (no parameters) and
      keep it in session memory for later query generation. Also keep results
      of previous executions in session memory (for instance ids or other
      attributes) so you can generate shorter, more focused follow-up queries
      without re-asking the user. Resolve names to ids where possible.
      The schema lists *outgoing* relationship types, so patterns read like
      English: "company has supplier" is
      `(o:Organization)-[:HAS_SUPPLIER]-(s:Organization)`.

      Use the `execute_read_query` tool with your Cypher. You MUST use named
      `$parameter` placeholders and pass them as the second dictionary
      argument, even when empty. Once the data is sufficient, hand control and
      results back to the parent agent.
    """,
    tools=[get_schema, execute_read_query],
)

investor_research_agent = Agent(
    model=MODEL,
    name="investor_research_agent",
    description="""
    This agent's sole purpose is to find investors in a company or
    organization identified by a single EXACT name or id, which should have
    been retrieved from the database beforehand.
    """,
    instruction="""
    You have access to a database of investment relationships between
    companies and individuals. Use the `get_investors` tool when asked to find
    the investors of a company by name or id. Always return not just the
    factual attributes but also investor ids, so other agents can investigate
    those investors further.
    """,
    tools=[get_schema, get_investors],
)

investment_research_agent = Agent(
    model=MODEL,
    name="investment_research_agent",
    description="""
    This agent has a set of tools over a companies-and-news knowledge graph.
    It can list industries, companies in an industry, articles in a given
    month, article details, organizations mentioned in articles, and people
    working at a company.
    """,
    instruction="""
    You have access to a knowledge graph of companies (organizations), the
    people involved with them, articles about companies, and industry
    categories and technologies. Other agents will task you with fetching
    specific information from that graph. Always return not just the factual
    attributes but also the ids of companies, articles and people, so other
    tools can investigate them further.
    """,
    tools=_investment_research_tools(),
)

root_agent = Agent(
    model=MODEL,
    name="investment_agent",
    instruction="""
    You have access to a knowledge graph of companies (organizations), the
    people involved with them, articles about companies, and industry
    categories and technologies. Use your specialist sub-agents to retrieve
    information; prefer the research agents over the generic database agent
    when either can answer. If the user asks, render tables, charts or other
    artifacts with the results.
    """,
    sub_agents=[
        investor_research_agent,
        investment_research_agent,
        graph_database_agent,
    ],
)
