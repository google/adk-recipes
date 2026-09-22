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

"""Memory layer: ADK Memory Bank wrapper, memory tool, auto-capture callback."""

from horizon.memory.add_memory_tool import (
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    add_memory_entry,
    memory,
)
from horizon.memory.auto_capture import auto_capture_callback
from horizon.memory.user_profile import (
    load_user_profile,
    render_user_profile,
)

__all__ = [
    "MEMORY_CHAR_LIMIT",
    "USER_CHAR_LIMIT",
    "add_memory_entry",
    "auto_capture_callback",
    "load_user_profile",
    "memory",
    "render_user_profile",
]
