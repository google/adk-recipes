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

from genmedia4commerce.core import retry
from genmedia4commerce.core.errors import (
    DeadlineExceeded,
    GenerationError,
    GenMediaError,
)
from genmedia4commerce.core.retry import (
    EMPTY_RESULT_ATTEMPTS,
    STALL_RETRIES,
    TRANSPORT_ATTEMPTS,
    Deadline,
    backoff_seconds,
    is_transient,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(retry.time, "monotonic", c)
    return c


def test_deadline_remaining_and_expired(clock):
    d = Deadline(10)
    assert d.remaining == 10
    assert not d.expired
    clock.now += 4
    assert d.remaining == pytest.approx(6)
    d.check()  # no raise
    clock.now += 7
    assert d.remaining == 0
    assert d.expired


def test_deadline_remaining_ms_is_at_least_one_second(clock):
    d = Deadline(10)
    clock.now += 2.5
    assert d.remaining_ms == 7500
    clock.now += 10
    assert d.remaining_ms == 1000


def test_deadline_check_raises(clock):
    d = Deadline(1)
    clock.now += 2
    with pytest.raises(DeadlineExceeded):
        d.check()


def test_deadline_exceeded_not_retryable():
    exc = DeadlineExceeded("late")
    assert isinstance(exc, GenerationError)
    assert isinstance(exc, GenMediaError)
    assert exc.retryable is False


def test_generation_error_retryable_flag():
    assert GenerationError("x").retryable is False
    assert GenerationError("x", retryable=True).retryable is True


@pytest.mark.parametrize(
    "message",
    [
        "Connection aborted.",
        "connection reset by peer",
        "[Errno 54] ECONNRESET",
        "OSError EADDRNOTAVAIL",
        "Broken pipe",
        "Remote end closed connection without response",
        "Server disconnected without sending a response",
        "Connection refused",
        "Resource temporarily unavailable",
        "The read operation timed out",
        "Error code: 400 - Request blocked due to copyright/recitation content.",
    ],
)
def test_transient_markers(message):
    assert is_transient(RuntimeError(message))


@pytest.mark.parametrize(
    "exc", [ConnectionError("x"), OSError("x"), TimeoutError("x")]
)
def test_transient_types(exc):
    assert is_transient(exc)


@pytest.mark.parametrize(
    "exc", [ValueError("bad input"), KeyError("k"), RuntimeError("400 bad")]
)
def test_not_transient(exc):
    assert not is_transient(exc)


def test_deadline_exceeded_is_not_transient():
    assert not is_transient(DeadlineExceeded("timed out"))


def test_backoff_seconds():
    assert [backoff_seconds(a) for a in (1, 2, 3)] == [1, 2, 4]


def test_attempt_constants():
    assert TRANSPORT_ATTEMPTS == 4
    assert EMPTY_RESULT_ATTEMPTS == 2
    assert STALL_RETRIES == 1


def test_deadline_restart_starts_the_same_budget_again(clock):
    d = Deadline(10)
    clock.now += 11
    assert d.expired
    d.restart()
    assert d.remaining == 10
    assert d.seconds == 10


def test_context_pool_carries_cancel_into_workers():
    """A pool job sees the submitting thread's CANCEL; a plain pool does not."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    event = threading.Event()
    token = retry.CANCEL.set(event)
    try:
        with retry.ContextPool(max_workers=2) as pool:
            seen = list(pool.map(lambda _: retry.CANCEL.get(), range(3)))
        with ThreadPoolExecutor(max_workers=1) as plain:
            plain_seen = plain.submit(retry.CANCEL.get).result()
    finally:
        retry.CANCEL.reset(token)
    assert seen == [event, event, event]
    assert plain_seen is None


def test_context_pool_job_stops_when_cancelled():
    """Setting CANCEL makes a Deadline inside a pool job report cancelled."""
    import threading

    event = threading.Event()
    token = retry.CANCEL.set(event)
    try:
        with retry.ContextPool(max_workers=1) as pool:
            before = pool.submit(lambda: Deadline(60).cancelled).result()
            event.set()
            after = pool.submit(lambda: Deadline(60).cancelled).result()
    finally:
        retry.CANCEL.reset(token)
    assert (before, after) == (False, True)


def test_deadline_run_returns_the_result_and_forwards_exceptions():
    assert Deadline(5).run(lambda: 42) == 42
    with pytest.raises(ValueError, match="boom"):
        Deadline(5).run(lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_deadline_run_gives_up_when_the_call_outlives_the_budget():
    """An HTTP read timeout never fires while bytes trickle in; we do."""
    import threading
    import time

    release = threading.Event()

    def slow():
        release.wait(10)
        return "late"

    deadline = Deadline(1.0)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="no answer within 1s"):
        deadline.run(slow, "judge")
    assert time.monotonic() - started < 3
    release.set()


def test_deadline_run_carries_the_context_into_the_helper_thread():
    import threading

    event = threading.Event()
    token = retry.CANCEL.set(event)
    try:
        assert Deadline(5).run(retry.CANCEL.get) is event
    finally:
        retry.CANCEL.reset(token)
