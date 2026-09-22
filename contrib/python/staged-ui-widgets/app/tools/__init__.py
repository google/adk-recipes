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
"""The agent's tools.

Every one of them stages and returns; none of them renders -- that is the
invariant this layout exists to make visible. Across ``app/``,
``render_ui_widget`` is called in exactly one place, ``staging/lifecycle.py``;
where this package names it, that is prose about the rule, not a use of it.
``test_no_tool_renders`` matches the call form across all of ``app/``, so the
check stays honest as those docstrings grow.
"""

from .orders import get_order_status
from .picks import compare_picks, get_personalized_picks
from .preferences import update_shopper_preference
from .recall import show_again
from .spend import get_spend_summary

ALL_TOOLS = [
    get_personalized_picks,
    compare_picks,
    get_order_status,
    get_spend_summary,
    update_shopper_preference,
    show_again,
]

__all__ = [
    "ALL_TOOLS",
    "compare_picks",
    "get_order_status",
    "get_personalized_picks",
    "get_spend_summary",
    "show_again",
    "update_shopper_preference",
]
