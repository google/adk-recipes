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

"""Code shared by the agent, the CLI, the dashboard and the deploy wrapper.

Holds the package version, the usage analytics headers, the engine routes and
the wire layer (transport, authentication and event normalization) through
which `ambient_quality_cli` (fetched into `.aqua` by `agents-cli`) and the
Cloud Run dashboard (`ui/`) talk to an agent deployment. Dependencies are
restricted to google-auth and httpx, so those clients run without importing
ADK or `ambient_quality_agent`.
"""

# Read by hatch as the distribution version, and sent in usage analytics headers.
__version__ = "0.2.0"
