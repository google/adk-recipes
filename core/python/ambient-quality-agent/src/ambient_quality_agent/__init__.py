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

from dotenv import load_dotenv

# Submodules read environment configuration at import time. Existing
# environment variables take precedence over `.env`.
load_dotenv()

from .core.orchestrator import build_orchestrator  # noqa: E402
from .core.workflow import investigation_workflow  # noqa: E402

root_agent = build_orchestrator()

__version__ = "0.1.0"
__all__ = ["__version__", "investigation_workflow", "root_agent"]
