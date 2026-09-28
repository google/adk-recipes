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

import app.environment_context as ec
from app.conversation.session_start import _build_environment
from app.environment import LocalEnvironment


class _DummyEnv(LocalEnvironment):
    def __init__(self, user_id: str):
        super().__init__()
        self.user_id = user_id


def test_provider_override_is_consulted_first():
    ec.set_environment_provider(_DummyEnv)
    try:
        env, pending = _build_environment("alice")
        assert isinstance(env, _DummyEnv)
        assert env.user_id == "alice"
        assert pending is None
    finally:
        ec.set_environment_provider(None)


def test_no_override_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    ec.set_environment_provider(None)
    env, _ = _build_environment("bob")
    assert isinstance(env, LocalEnvironment)
