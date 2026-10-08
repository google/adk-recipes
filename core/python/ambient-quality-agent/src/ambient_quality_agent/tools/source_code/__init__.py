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

"""Observed agent source code access for root-cause analysis.

Layers:
* `snapshot`: GCS storage layout, manifest models, caps, and ignore patterns.
* `reader`: GCS client for reading revisions, manifests, file slices, and regex searches.
* `agent_source_browsing_tools`: ADK tools and session-state keys for agent inspection.

All operations in this package are read-only; snapshots are published during deployment.
"""
