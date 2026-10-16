# Copyright 2025 Google LLC
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

"""Import-time snapshot of app.config + restore helper (Phase 16 open item 6).

tests/conftest.py imports app.config at collection time — BEFORE the session
env fixture pins fake values (GCS_BUCKET=test-bucket, ...). Tests that clean
up with importlib.reload(app.config) used to re-derive module values under
the PINNED env, permanently flipping e.g. app.config.GCS_BUCKET from None to
"test-bucket" for the rest of the process. Restoring this snapshot returns
the module to exactly its import-time state, deterministically, regardless
of what the environment looks like at cleanup time.
"""

import app.config as _config_module

BASELINE = {
    name: value
    for name, value in vars(_config_module).items()
    if not name.startswith("__")
}


def restore_config_baseline() -> None:
    """Return app.config to its import-time state (no reload, no env reads)."""
    for name, value in BASELINE.items():
        setattr(_config_module, name, value)
