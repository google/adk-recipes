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

import inspect

import app.environment.process as proc


def test_process_handle_is_in_environment_layer():
    assert proc.ProcessHandle.__module__ == "app.environment.process"


def test_backend_gone_error_is_exception():
    assert issubclass(proc.BackendGoneError, Exception)


def test_process_module_imports_nothing_from_tools_or_sandbox():
    src = inspect.getsource(proc)
    assert "app.tools" not in src
    assert "app.sandbox" not in src


def test_base_env_module_has_no_tools_import():
    from app.environment import base

    assert "from app.tools" not in inspect.getsource(base)
