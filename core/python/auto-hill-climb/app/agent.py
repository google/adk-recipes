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

import os
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import Client, types

MODEL = os.environ.get("MODEL_NAME", "gemini-3.8-flash")


def lookup_account(account_id: str) -> dict[str, Any]:
    """Look up account details, including current discount status and billing cycle.

    Args:
        account_id: The ID of the account.
    """
    if account_id == "ACC-123":
        return {
            "status": "active",
            "already_discounted": True,
            "billing_cycle_end": "2026-11-01",
        }
    return {
        "status": "active",
        "already_discounted": False,
        "billing_cycle_end": "2026-11-15",
    }


def offer_retention(offer_code: str) -> str:
    """Offer a retention discount or promotion to the user.

    Args:
        offer_code: The code for the retention offer (e.g. 'FREE_MONTH').
    """
    return f"Applied retention offer {offer_code}. Waiting for user confirmation."


def cancel_subscription(reason: str) -> str:
    """Cancel the user's subscription. Returns the effective cancellation date.

    Args:
        reason: The reason for cancellation.
    """
    return f"Subscription cancelled for reason: {reason}. Effective date: 2026-11-15."


def escalate_to_human(issue_type: str) -> str:
    """Escalate the current issue to a human support agent.

    Args:
        issue_type: The type of issue being escalated (e.g. 'REFUND', 'TECHNICAL').
    """
    return f"Issue type {issue_type} escalated to human agent. A representative will join shortly."


client = Client(
    vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location="global"
)

root_agent = Agent(
    name="support_agent",
    model=Gemini(
        model=MODEL,
        client=client,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a customer support agent. Help the user. If they want to cancel, maybe offer them a discount. If they want a refund, handle it.""",
    tools=[lookup_account, offer_retention, cancel_subscription, escalate_to_human],
)

app = App(
    root_agent=root_agent,
    name="app",
)
