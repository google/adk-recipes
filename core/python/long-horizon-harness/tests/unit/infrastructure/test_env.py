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

from app.infrastructure.env import env_flag


def test_absent_uses_default(monkeypatch):
    monkeypatch.delenv("LHA_X", raising=False)
    assert env_flag("LHA_X") is False
    assert env_flag("LHA_X", default=True) is True


def test_truthy_and_falsy(monkeypatch):
    for v in ("1", "true", "TRUE", "Yes", " true "):
        monkeypatch.setenv("LHA_X", v)
        assert env_flag("LHA_X") is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("LHA_X", v)
        assert env_flag("LHA_X", default=True) is False
