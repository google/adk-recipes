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

"""Error types shared by core, eval, workflows and adapters, and what a
client may read of a failure (``user_message``)."""

__all__ = [
    "SERVER_FAILED",
    "USER_ERRORS",
    "BusyError",
    "ConfigError",
    "DeadlineExceeded",
    "EvalError",
    "GenMediaError",
    "GenerationError",
    "InputError",
    "StorageError",
    "user_message",
]


class GenMediaError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(GenMediaError):
    """Missing or invalid settings."""


class InputError(GenMediaError):
    """Bad caller input (maps to HTTP 400)."""


class GenerationError(GenMediaError):
    """A model call failed after retries."""

    def __init__(self, message: str, *, retryable: bool = False):
        """Store the message and whether a later retry may succeed."""
        super().__init__(message)
        self.retryable = retryable


class DeadlineExceeded(GenerationError):
    """The call's deadline expired (never retryable)."""

    def __init__(self, message: str):
        """Create a non-retryable deadline error."""
        super().__init__(message, retryable=False)


class EvalError(GenMediaError):
    """An evaluator could not produce a verdict (transient)."""


class StorageError(GenMediaError):
    """A stored document exists but could not be read."""


class BusyError(GenMediaError):
    """The server could not do it now and the user may try again (maps to
    HTTP 503); unlike ``StorageError``, its message is meant for the user."""


#: Errors whose message is meant for the user; the others (storage,
#: configuration, unexpected) can name buckets, paths or credentials.
USER_ERRORS = (InputError, GenerationError, EvalError, BusyError)
#: What a client reads of any other failure (log its details).
SERVER_FAILED = "the generation failed on the server; try again"


def user_message(exc: BaseException, fallback: str = SERVER_FAILED) -> str:
    """What a client (an event, a job, a tool result, the agent's model) may
    read of ``exc``: its message when it is meant for the user
    (``USER_ERRORS``), else ``fallback``; the caller logs the details."""
    if isinstance(exc, USER_ERRORS) and str(exc):
        return str(exc)
    return fallback
