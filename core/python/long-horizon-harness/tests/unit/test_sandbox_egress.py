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

"""Unit tests for the egress allowlist plumbing (Layer A — exfil_config)."""

from __future__ import annotations


def test_curated_default_allowlist_coverage():
    from app.guardrails.exfil_config import default_config, host_allowed

    cfg = default_config()
    for h in [
        "github.com",
        "raw.githubusercontent.com",
        "drive.google.com",
        "docs.google.com",
        "storage.googleapis.com",
        "drive.googleapis.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    ]:
        assert host_allowed(h, cfg), f"{h} should be allowlisted"
    for h in ["evil.example.com", "pastebin.com", "ngrok.io", "notgithub.com"]:
        assert not host_allowed(h, cfg), f"{h} should NOT be allowlisted"
