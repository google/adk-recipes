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
"""Unit test for the A2A agent-card compatibility modifier."""

import asyncio

from a2a.types import AgentCard, AgentInterface

from app.app_utils.a2a import _add_v0_3_compat_interface


def test_v0_3_interface_is_added_once():
    card = AgentCard(
        name="planner",
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url="http://x/a2a")
        ],
    )
    for _ in range(3):  # the modifier runs on every card request
        card = asyncio.run(_add_v0_3_compat_interface(card))
    versions = [i.protocol_version for i in card.supported_interfaces]
    assert versions.count("0.3") == 1
    assert len(card.supported_interfaces) == 2
