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

"""Feedback schema — qualitative text plus an optional thumbs sentiment.

The feedback box is qualitative: a free-text suggestion with an optional
thumbs-up/down ``sentiment``, no numeric rating. The ``Feedback`` model must
accept a text-only or thumbs-only payload and reject a stray ``score`` (a
removed numeric-rating field) rather than silently dropping it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.feedback.models import Feedback


def test_text_only_payload_validates() -> None:
    fb = Feedback(text="please add dark mode")
    assert fb.text == "please add dark mode"
    assert fb.log_type == "feedback"
    assert fb.service_name == "lha"
    assert fb.session_id  # auto-generated uuid


def test_text_defaults_to_empty_string() -> None:
    assert Feedback().text == ""


def test_sentiment_defaults_to_none() -> None:
    assert Feedback().sentiment is None


def test_thumbs_sentiment_validates() -> None:
    assert Feedback(sentiment="up").sentiment == "up"
    assert Feedback(text="nope", sentiment="down").sentiment == "down"


def test_invalid_sentiment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Feedback.model_validate({"sentiment": "meh"})


def test_include_context_defaults_false() -> None:
    assert Feedback().include_context is False


def test_include_context_opt_in_validates() -> None:
    assert Feedback(include_context=True).include_context is True


def test_score_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Feedback.model_validate({"text": "hi", "score": 4})
