# Copyright 2025 Google LLC
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

"""Sequential Google Trends pipeline: generate BigQuery SQL, then execute it."""

import os

from google.adk.agents import LlmAgent, SequentialAgent

from app.prompt import load_agent_instructions
from app.tools import execute_bigquery_sql

# Model names are read from the environment (see .env.example). No default is
# hardcoded here: the repo requires model names to come from env vars only.
MODEL_AGENT = os.getenv("MODEL_NAME_AGENT")
MODEL_TOOL = os.getenv("MODEL_NAME_TOOL")

# --- Dynamically load agent instructions ---
full_instruction = load_agent_instructions()


# --- 1. Define Sub-Agents for the Pipeline ---

# Google Trends SQL Generator Agent
# Takes the user's question and generates a BigQuery SQL query.
trends_query_generator_agent = LlmAgent(
    name="TrendsQueryGeneratorAgent",
    model=MODEL_AGENT,
    instruction=full_instruction,
    description="Generates a BigQuery SQL query based on the user's question about Google Trends.",
    output_key="generated_sql",  # Stores output in state['generated_sql']
)

# Google Trends SQL Executor Agent
# Takes the generated SQL from the state and executes it using a tool.
trends_query_executor_agent = LlmAgent(
    name="TrendsQueryExecutorAgent",
    model=MODEL_TOOL,
    # This instruction tells the agent how to use the state and the tool.
    instruction="""You are a SQL execution agent.
Your task is to execute the BigQuery SQL query provided in the `{generated_sql}` placeholder.
Use the execute_bigquery_sql tool to run the query.
The query is already written; do not modify it. Simply pass it to the tool.
Read the query results and give insights to the user.
""",
    description="Executes the generated SQL query using the execute_bigquery_sql tool.",
    tools=[execute_bigquery_sql],
)

# --- 2. Create the SequentialAgent ---
# This agent orchestrates the pipeline by running the sub_agents in order.
root_agent = SequentialAgent(
    name="GoogleTrendsAgent",
    sub_agents=[trends_query_generator_agent, trends_query_executor_agent],
    description="""A two-step pipeline that first generates a SQL query for Google Trends and then executes it.
    Format the output as user friendly markdown format. Separate the SQL query and the interpretation of the results with a horizontal line.""",
)
