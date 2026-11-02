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

"""What a client may read of a failure (``core.errors.user_message``)."""

import pytest

from genmedia4commerce.core.errors import (
    SERVER_FAILED,
    BusyError,
    ConfigError,
    DeadlineExceeded,
    EvalError,
    GenerationError,
    InputError,
    StorageError,
    user_message,
)


@pytest.mark.parametrize(
    "exc",
    [
        InputError("person_image is required"),
        GenerationError("the image model refused the request"),
        DeadlineExceeded("timed out after 60s"),
        EvalError("the judge gave no verdict"),
        BusyError("The generation could not be started; try again."),
    ],
)
def test_errors_meant_for_the_user_keep_their_message(exc):
    assert user_message(exc) == str(exc)


@pytest.mark.parametrize(
    "exc",
    [
        StorageError("could not read gs://bucket/sessions/x/state.json"),
        ConfigError("PROJECT_ID is not set"),
        RuntimeError("key=secret at /srv/app.py"),
        KeyError("token"),
        InputError(""),  # nothing to say: the generic message
    ],
)
def test_any_other_failure_gets_the_generic_message(exc):
    assert user_message(exc) == SERVER_FAILED
    assert user_message(exc, "the tool failed") == "the tool failed"
