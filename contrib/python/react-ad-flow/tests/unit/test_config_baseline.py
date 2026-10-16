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

"""Phase 16 open item 6: reload-under-pinned-env must not leak into app.config."""

import importlib

import app.config as config_module
from tests._config_baseline import BASELINE, restore_config_baseline


def test_restore_after_reload_under_pinned_env(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET", "leaky-bucket")
    importlib.reload(config_module)
    assert config_module.GCS_BUCKET == "leaky-bucket"  # the leak, mid-test
    monkeypatch.undo()
    restore_config_baseline()
    assert config_module.GCS_BUCKET == BASELINE["GCS_BUCKET"]


def test_baseline_covers_env_derived_values():
    # GOOGLE_CLOUD_PROJECT is not an app.config module attribute (read
    # directly via os.environ elsewhere) — verified against app/config.py.
    for key in ("GCS_BUCKET", "MODEL", "IMAGE_GENERATION", "DB_PATH"):
        assert key in BASELINE
