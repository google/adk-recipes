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

"""Sequential analyst → critic → report_writer crew using Workflow.

Three LlmAgents wired in a straight line with ADK's Workflow class.
Each agent receives the previous agent's output as its node_input.
No file I/O: the sales dataset is embedded as a constant string and
returned by the get_sales_data tool used by the analyst.
"""

from google.adk import Agent
from google.adk import Workflow

# ---------------------------------------------------------------------------
# Synthetic sales data (4 regions × 3 months = 12 rows)
# ---------------------------------------------------------------------------

SALES_CSV = """\
region,month,revenue_usd,units_sold
North,January,142500,1140
North,February,138200,1105
North,March,159800,1278
South,January,98700,789
South,February,87300,698
South,March,104600,837
East,January,211400,1690
East,February,224800,1798
East,March,198600,1589
West,January,76200,610
West,February,81500,652
West,March,79400,635
"""


# ---------------------------------------------------------------------------
# Tool: expose the embedded CSV to the analyst
# ---------------------------------------------------------------------------


def get_sales_data() -> str:
    """Return a 12-row CSV of regional monthly sales figures."""
    return SALES_CSV


# ---------------------------------------------------------------------------
# Sub-agents
# ---------------------------------------------------------------------------

analyst = Agent(
    name="analyst",
    model="gemini-2.5-flash",
    instruction="""You are a data analyst.

Use the get_sales_data tool to fetch the sales CSV, then:
1. Compute total revenue per region across all three months.
2. Identify the top-performing and bottom-performing region by total revenue.
3. Compute month-over-month revenue change for each region.

Present your findings as a structured analysis with clearly labelled
sections: Regional Totals, Top/Bottom Performers, and Month-over-Month
Trends. Be precise — include the actual numbers.""",
    tools=[get_sales_data],
)

critic = Agent(
    name="critic",
    model="gemini-2.5-flash",
    instruction="""You are a critical analyst reviewing a data analysis report.

The analysis you are reviewing:
{node_input}

Challenge the analysis on at least three of the following dimensions:
- Sample size limitations (only 3 months of data)
- Seasonal effects that could distort the ranking
- Missing context (market size, cost structure, headcount)
- Variance significance given the small sample
- Metrics that might tell a different story (units vs. revenue)

Be constructive but direct. Each challenge should be a separate
paragraph. Do not repeat the original numbers back at length — focus
on what the analyst missed or overstated.""",
    tools=[],
)

report_writer = Agent(
    name="report_writer",
    model="gemini-2.5-flash",
    instruction="""You are an executive communications specialist.

You have access to an analyst report followed by a critical review.
Your input contains both:
{node_input}

Write a concise executive summary (aim for 200-300 words) that:
1. Leads with the most important finding from the analyst.
2. Acknowledges the top one or two limitations raised by the critic.
3. Closes with a single recommended next step for leadership.

Use plain prose. No bullet points. No section headers. Write as if
this will be read by a VP of Sales who has two minutes to spare.""",
    tools=[],
)

# ---------------------------------------------------------------------------
# Workflow: analyst → critic → report_writer
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="sales_analysis_crew",
    edges=[
        ("START", analyst, critic, report_writer),
    ],
)
