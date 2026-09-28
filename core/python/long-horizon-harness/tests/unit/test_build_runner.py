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

from google.adk.runners import Runner

from app.fast_api_app import build_runner


def test_build_runner_returns_runner_under_in_memory_env(monkeypatch):
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("USE_IN_MEMORY_TASK_STORE", "true")
    runner = build_runner()
    assert isinstance(runner, Runner)
    assert runner.app is not None
