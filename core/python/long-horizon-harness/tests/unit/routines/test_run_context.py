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

from app.routines.run_context import (
    RoutineRun,
    active_routine_run,
    reset_routine_run,
    set_routine_run,
)


def test_unset_by_default():
    assert active_routine_run() is None


def test_set_and_reset():
    run = RoutineRun(routine_id="digest", owner="alice@x")
    token = set_routine_run(run)
    try:
        assert active_routine_run() == run
    finally:
        reset_routine_run(token)
    assert active_routine_run() is None
