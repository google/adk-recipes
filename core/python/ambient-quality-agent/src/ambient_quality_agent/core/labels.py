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

"""The billing label AQA stamps on everything it spends money on.

Cloud Billing has no "who called it" dimension: the finest native cut of the
billing export is ``project + service + SKU (+ resource)`` plus *your* labels.
An observed agent and AQA both deployed on Agent Runtime roll up under the same
billing service and largely the same SKUs, so neither dimension separates
them -- attribution has to come from a label.

One label, everywhere: ``component=aqa``. The same key-value pair is applied to
resources (buckets, BigQuery datasets, and the Agent Runtime agent; see ``extension/README.md``
and ``terraform/modules/aqa``), Gemini requests (`LocatedGemini` and direct GenAI
calls in insight clustering), and BigQuery jobs, grouping infrastructure and model
costs within a single Cloud Billing report.
"""

from __future__ import annotations

BILLING_LABEL_KEY = "component"
"""Label key applied to every AQA-owned resource, request, and job."""

BILLING_LABEL_VALUE = "aqa"
"""Label value identifying this system in Cloud Billing."""

BILLING_LABELS: dict[str, str] = {BILLING_LABEL_KEY: BILLING_LABEL_VALUE}
"""The full label map, as a template. Copy it -- never mutate it in place."""


def build_request_labels() -> dict[str, str]:
    """Returns a fresh copy of the billing labels for one request or job.

    Callers hand the map to SDK request/config objects that may keep a reference
    to (and mutate) it.

    Returns:
        Fresh dictionary copy of the billing labels.
    """
    return dict(BILLING_LABELS)
