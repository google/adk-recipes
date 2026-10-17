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
"""Bearer-token check for admin endpoints. INTENTIONALLY VULNERABLE sample.

The service is expected to read its admin token from the ORDERS_API_TOKEN
environment variable and to reject every request when it is unset.
"""

# VULNERABLE: a credential committed to source control.
ADMIN_TOKEN = "orders-admin-change-me"  # noqa: S105 -- intentional sample vulnerability


def is_admin(authorization_header: str | None) -> bool:
    """Returns True if the request carries the admin bearer token."""
    if not authorization_header:
        return False
    return authorization_header == f"Bearer {ADMIN_TOKEN}"
