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

"""Shared constant for the GCS layout of exported telemetry spans.

This module is shared between the agent and the vendored CLI, which runs with
minimal dependencies.
"""

from __future__ import annotations

SPAN_OBJECT_PREFIX = "aqa-telemetry/spans"
"""GCS object prefix for exported spans, without a trailing slash."""
