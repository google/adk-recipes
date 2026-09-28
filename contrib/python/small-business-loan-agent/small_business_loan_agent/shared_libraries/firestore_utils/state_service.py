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

"""
State management service for human-in-the-loop workflow using Firestore.

Enables persistent state tracking, human approval workflows, and resume
capability after human intervention for each loan_request_id.
"""

import os
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)

GCP_FIRESTORE_DB = "small-business-loan-states"


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert datetime objects to ISO format strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_serializable(item) for item in obj]
    return obj


class ProcessStateService:
    """Service for managing process states in Firestore."""

    # Step names matching agent names
    STEP_DOCUMENT_EXTRACTION = "DocumentExtractionAgent"
    STEP_UNDERWRITING = "UnderwritingAgent"
    STEP_PRICING = "PricingAgent"
    STEP_LOAN_DECISION = "LoanDecisionAgent"

    ALL_STEPS = (
        STEP_DOCUMENT_EXTRACTION,
        STEP_UNDERWRITING,
        STEP_PRICING,
        STEP_LOAN_DECISION,
    )

    # Status values
    STATUS_NOT_STARTED = "not_started"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_PENDING_APPROVAL = "pending_approval"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_ERROR = "error"

    # Overall process statuses
    OVERALL_STATUS_ACTIVE = "active"
    OVERALL_STATUS_PENDING_APPROVAL = "pending_approval"
    OVERALL_STATUS_COMPLETED = "completed"
    OVERALL_STATUS_FAILED = "failed"

    def __init__(self, database_name: str | None = None) -> None:
        db_name = database_name or GCP_FIRESTORE_DB
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")

        if not project_id:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT environment variable must be set"
            )

        self.db = firestore.Client(project=project_id, database=db_name)
        self.collection_name = "process_states"

    def create_process(
        self, request_id: str, session_id: str
    ) -> dict[str, Any]:
        """Initialize a new process state in Firestore."""
        now = firestore.SERVER_TIMESTAMP

        steps = {}
        for step_name in self.ALL_STEPS:
            steps[step_name] = {
                "status": self.STATUS_NOT_STARTED,
                "completed_at": None,
                "data": None,
                "human_review_notes": None,
                "approved_by": None,
                "approved_at": None,
            }

        process_state = {
            "loan_request_id": request_id,
            "session_id": session_id,
            "current_step": self.STEP_DOCUMENT_EXTRACTION,
            "overall_status": self.OVERALL_STATUS_ACTIVE,
            "created_at": now,
            "updated_at": now,
            "steps": steps,
            "issues": [],
        }

        doc_ref = self.db.collection(self.collection_name).document(request_id)
        doc_ref.set(process_state)

        logger.info(f"Created process state for request_id: {request_id}")
        return process_state

    def update_step_status(
        self,
        request_id: str,
        step_name: str,
        status: str,
        data: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update the status of a specific step."""
        doc_ref = self.db.collection(self.collection_name).document(request_id)

        update_data: dict[str, Any] = {
            f"steps.{step_name}.status": status,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if status == self.STATUS_COMPLETED:
            update_data[f"steps.{step_name}.completed_at"] = (
                firestore.SERVER_TIMESTAMP
            )

        if data is not None:
            update_data[f"steps.{step_name}.data"] = data

        if error_message:
            update_data[f"steps.{step_name}.error_message"] = error_message

        if status == self.STATUS_COMPLETED:
            next_step = self._get_next_step(step_name)
            if next_step:
                update_data["current_step"] = next_step

        doc_ref.update(update_data)
        logger.info(
            f"Updated step {step_name} to status {status} for request_id: {request_id}"
        )

    def mark_step_for_review(
        self,
        request_id: str,
        step_name: str,
        issue_description: str,
        missing_fields: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Mark a step as requiring human review."""
        doc_ref = self.db.collection(self.collection_name).document(request_id)

        current_time = datetime.now(UTC)
        issue: dict[str, Any] = {
            "step": step_name,
            "issue_type": "requires_review",
            "description": issue_description,
            "raised_at": current_time,
            "resolved": False,
            "resolved_at": None,
            "resolved_by": None,
        }

        if missing_fields:
            issue["missing_fields"] = missing_fields

        update_data: dict[str, Any] = {
            f"steps.{step_name}.status": self.STATUS_PENDING_APPROVAL,
            "overall_status": self.OVERALL_STATUS_PENDING_APPROVAL,
            "issues": firestore.ArrayUnion([issue]),
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if data is not None:
            update_data[f"steps.{step_name}.data"] = data

        doc_ref.update(update_data)
        logger.info(
            f"Marked step {step_name} for review for request_id: {request_id}"
        )

    def can_proceed_to_step(self, request_id: str, step_name: str) -> bool:
        """Check if the process can proceed to a given step."""
        doc_ref = self.db.collection(self.collection_name).document(request_id)
        doc = doc_ref.get()

        if not doc.exists:
            return False

        data = doc.to_dict()
        if not data:
            return False

        steps = data.get("steps", {})

        try:
            step_index = self.ALL_STEPS.index(step_name)
        except ValueError:
            return False

        for i in range(step_index):
            prev_step_name = self.ALL_STEPS[i]
            prev_step = steps.get(prev_step_name, {})
            prev_status = prev_step.get("status")

            if prev_status not in [self.STATUS_COMPLETED, self.STATUS_APPROVED]:
                return False

        return True

    def get_process_status(self, request_id: str) -> dict[str, Any] | None:
        """Get the current status of a process."""
        doc_ref = self.db.collection(self.collection_name).document(request_id)
        doc = doc_ref.get()

        if not doc.exists:
            return None

        return _make_json_serializable(doc.to_dict())

    def mark_process_complete(self, request_id: str) -> None:
        """Mark the entire process as complete."""
        doc_ref = self.db.collection(self.collection_name).document(request_id)

        doc_ref.update(
            {
                "current_step": None,
                "overall_status": self.OVERALL_STATUS_COMPLETED,
                "completed_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )

        logger.info(f"Marked process complete for request_id: {request_id}")

    def _get_next_step(self, current_step: str) -> str | None:
        """Get the next step in the workflow."""
        try:
            current_index = self.ALL_STEPS.index(current_step)
            if current_index < len(self.ALL_STEPS) - 1:
                return self.ALL_STEPS[current_index + 1]
            return None
        except ValueError:
            return None
