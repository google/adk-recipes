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

"""The permission model for this recipe — written by hand, on purpose.

attenu-guard does not decide what a task needs. You declare three
things and it enforces them:

1. `COORDINATOR` — what the root agent holds.
2. `DELEGATIONS`  — what each sub-agent may *request*. The child receives
   the meet of the request and the parent, so a request can only ever
   shrink the child, never widen it.
3. `TOOLS`        — the scope, and the quantities the ceilings are
   measured against, that each tool call needs.

An agent missing from `DELEGATIONS` gets nothing. A tool missing from
`TOOLS` is checked against a scope no authority grants. Both fail closed.
"""

from attenu_guard import Authority, EgressRank, RowLimit
from attenu_guard.adapters.google_adk import ToolAuthority

# --------------------------------------------------------------------
# Row-limit and TTL constants, named so the numbers below read as
# decisions rather than magic numbers.
# --------------------------------------------------------------------
COORDINATOR_MAX_ROWS = 10_000
COORDINATOR_TTL_SECONDS = 3600  # 1 hour

BILLING_MAX_ROWS = 500
BILLING_TTL_SECONDS = 900  # 15 minutes

# Deliberately absurd, so GREEDY_REQUEST (below) cannot be satisfied by
# accident.
GREEDY_MAX_ROWS = 1_000_000
GREEDY_TTL_SECONDS = 999_999

# --------------------------------------------------------------------
# 1. What the coordinator holds.
#
# It can read orders, do anything in billing (including refunds), email
# a customer, and hand work to a sub-agent. Egress up to "any", 10k rows.
# --------------------------------------------------------------------
COORDINATOR = Authority(
    scopes={"orders.read", "billing.*", "mail.send", "agent.delegate.*"},
    ceilings=[RowLimit(COORDINATOR_MAX_ROWS), EgressRank("any")],
    ttl=COORDINATOR_TTL_SECONDS,
)

# --------------------------------------------------------------------
# 2. What each sub-agent may request.
#
# The billing agent looks invoices up. It asks for read access only: no
# refunds, no mail, no onward hand-off, 500 rows, no egress, 15 minutes.
# --------------------------------------------------------------------
DELEGATIONS = {
    "billing_agent": Authority(
        scopes={"billing.read"},
        ceilings=[RowLimit(BILLING_MAX_ROWS), EgressRank("none")],
        ttl=BILLING_TTL_SECONDS,
    ),
}

# A deliberately greedy request, used by the demo to show that asking for
# more than the parent holds does not produce more. Not wired into the
# agent tree.
GREEDY_REQUEST = Authority(
    scopes={"billing.*", "admin.root"},
    ceilings=[RowLimit(GREEDY_MAX_ROWS), EgressRank("any")],
    ttl=GREEDY_TTL_SECONDS,
)

# --------------------------------------------------------------------
# 3. What each tool call needs.
# --------------------------------------------------------------------
TOOLS = {
    "lookup_order": ToolAuthority("orders.read", lambda _: {"rows": 1}),
    "get_invoice": ToolAuthority("billing.read", lambda _: {"rows": 1}),
    "issue_refund": ToolAuthority(
        "billing.refund", lambda _: {"egress": "internal"}
    ),
    "email_customer": ToolAuthority("mail.send", lambda _: {"egress": "any"}),
}

# Every hand-off is itself checked, as `agent.delegate.<target>`, against
# the authority of the agent doing the handing off. The coordinator holds
# `agent.delegate.*`; the billing agent holds no delegate scope at all, so
# it cannot pass the task on to anyone.
DELEGATION_SCOPE = "agent.delegate"
