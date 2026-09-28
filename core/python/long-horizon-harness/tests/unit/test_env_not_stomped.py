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

"""Building the agent must not clobber GCP env the consumer already set."""

import os
import subprocess
import sys


def test_create_does_not_clobber_preset_cloud_location():
    # agent.py used to unconditionally set GOOGLE_CLOUD_LOCATION='global' at
    # import; a consumer pinning a region (e.g. us-central1 for a regional model)
    # would silently lose it. Run in a subprocess so the one-shot module import
    # is observed cleanly.
    env = dict(os.environ)
    env["GOOGLE_CLOUD_LOCATION"] = "us-central1"
    env["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
    code = (
        "import os; import app.agent; "
        "print(os.environ['GOOGLE_CLOUD_LOCATION']); "
        "print(os.environ['GOOGLE_GENAI_USE_VERTEXAI'])"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 0, f"STDOUT={r.stdout}\nSTDERR={r.stderr}"
    lines = r.stdout.strip().splitlines()
    assert lines[-2] == "us-central1"
    assert lines[-1] == "False"
