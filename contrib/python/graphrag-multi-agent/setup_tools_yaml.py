#!/usr/bin/env python3
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

"""Generate app/.adk/tools.yaml (the MCP Toolbox config) from .env.

The generated file embeds the Neo4j credentials from your environment, so it
is git-ignored and produced on demand rather than committed. Run this whenever
your credentials change:

    python setup_tools_yaml.py

The queries below are the pre-validated tools the investment_research_agent
serves through the MCP Toolbox; only the Neo4j source block is filled from the
environment.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OUTPUT = Path(__file__).parent / "app" / ".adk" / "tools.yaml"

# Values come from .env (see .env.example for the demo defaults). All four are
# required: a missing one would otherwise be written to the config as the
# literal string "None".
_REQUIRED = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
)

# The pre-validated query tools. A plain (non-f) string: the Cypher contains
# literal ``{`` / ``}`` and ``$param`` tokens that must reach the file verbatim.
_TOOLS = r"""
tools:
  companies_in_industry:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (:IndustryCategory {name:$industry})<-[:HAS_CATEGORY]-(c)
      WHERE NOT EXISTS { (c)<-[:HAS_SUBSIDIARY]-() }
      RETURN c.id as company_id, c.name as name, c.summary as summary
    description: Companies (company_id, name, summary) in a given industry
    parameters:
      - name: industry
        type: string
        description: Industry name to filter companies by

  companies:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      CALL db.index.fulltext.queryNodes("companies_fulltext", $search)
      YIELD node AS c, score
      RETURN c.id as company_id, c.name as name, c.summary as summary
      LIMIT 10
    description: List of Companies (id, name, summary) matching search text
    parameters:
      - name: search
        type: string
        description: Full-text search query for company names

  industries:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (i:IndustryCategory)
      RETURN DISTINCT i.name as industry_name
      ORDER BY i.name
    description: List of Industry names
    parameters: []

  articles_in_month:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (a:Article)
      WHERE date($date) <= date(a.date) < date($date) + duration('P1M')
      RETURN a.id as article_id, a.author as author, a.title as title,
             toString(a.date) as date, a.sentiment as sentiment
      LIMIT 25
    description: Articles (id, author, title, date, sentiment) in a month
    parameters:
      - name: date
        type: string
        description: Start date in yyyy-mm-dd format

  article:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (a:Article {id: $article_id})
      RETURN a.id as article_id, a.author as author, a.title as title,
             toString(a.date) as date, a.sentiment as sentiment,
             a.site as site, a.summary as summary, a.content as content
    description: Single Article details by article ID
    parameters:
      - name: article_id
        type: string
        description: Article ID to fetch

  companies_in_articles:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (o:Organization)<-[:MENTIONS]-(a:Article)
      WHERE a.id IN $article_ids
      RETURN DISTINCT o.id as company_id, o.name as name, o.summary as summary
    description: Companies mentioned in articles by article IDs
    parameters:
      - name: article_ids
        type: array
        description: List of article IDs

  people_at_company:
    kind: neo4j-cypher
    source: companies-graph
    statement: |
      MATCH (p:Person)-[r]-(o:Organization {id: $company_id})
      WHERE type(r) IN ["HAS_CEO", "HAS_BOARD_MEMBER"]
      RETURN p.name as name, type(r) as role
    description: People (name, role) associated with a company by company ID
    parameters:
      - name: company_id
        type: string
        description: Company ID to find people for
"""


def main() -> None:
    missing = [name for name in _REQUIRED if not os.getenv(name)]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ".\nCopy .env.example to .env and fill them in before running "
            "this script."
        )

    # json.dumps produces a correctly quoted/escaped scalar. JSON is a subset
    # of YAML, so this is valid YAML and safe even if a credential contains a
    # quote or backslash.
    sources = f"""sources:
  companies-graph:
    kind: "neo4j"
    uri: {json.dumps(os.getenv("NEO4J_URI"))}
    user: {json.dumps(os.getenv("NEO4J_USERNAME"))}
    password: {json.dumps(os.getenv("NEO4J_PASSWORD"))}
    database: {json.dumps(os.getenv("NEO4J_DATABASE"))}
"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(sources + _TOOLS, encoding="utf-8")
    # Confirm the write without echoing the URI, which may embed inline
    # credentials. The database name is not sensitive.
    print(f"Generated {OUTPUT} (database: {os.getenv('NEO4J_DATABASE')})")


if __name__ == "__main__":
    main()
