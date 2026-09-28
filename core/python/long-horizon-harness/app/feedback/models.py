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
import uuid
from typing import (
    Literal,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Feedback(BaseModel):
    """Represents feedback for a conversation."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = ""
    sentiment: Literal["up", "down"] | None = None
    include_context: bool = False
    log_type: Literal["feedback"] = "feedback"
    service_name: Literal["lha"] = "lha"
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
