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

import pytest

from app.guardrails.permission_guard import _HEADLESS
from app.routines.isolation import HEADLESS_PREAMBLE, routine_isolation
from app.routines.run_context import active_routine_run
from app.secrets.inject import _routine_secret_scope


def test_routine_isolation_sets_and_resets():
    assert active_routine_run() is None
    with routine_isolation("digest", owner="alice@x", secrets=("STRIPE_KEY",)):
        run = active_routine_run()
        assert run is not None
        assert run.routine_id == "digest" and run.owner == "alice@x"
        assert _HEADLESS.get() is True
        assert _routine_secret_scope.get() == frozenset({"STRIPE_KEY"})
    assert active_routine_run() is None
    assert _HEADLESS.get() is False
    assert _routine_secret_scope.get() is None


def test_routine_isolation_resets_on_exception():
    with pytest.raises(RuntimeError):
        with routine_isolation("d", owner="a", secrets=()):
            raise RuntimeError("boom")
    assert active_routine_run() is None
    assert _HEADLESS.get() is False
    assert _routine_secret_scope.get() is None


def test_headless_preamble_mentions_routine_id():
    assert "digest" in HEADLESS_PREAMBLE.format(routine_id="digest")
