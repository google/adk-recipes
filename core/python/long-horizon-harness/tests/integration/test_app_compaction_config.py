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

"""Lock the App-level compaction wiring.

Asserts the production ``App(...)`` exposes both trigger modes of
``EventsCompactionConfig`` and uses the lha summarizer subclass. End-to-end
compaction behavior (does the summary preserve user intent, does the model
stay coherent across compactions) belongs in evals, not pytest.
"""

from __future__ import annotations

from app.agent import app
from app.context.summarizer import HorizonSummarizer


def test_app_has_events_compaction_config():
    config = app.events_compaction_config
    assert config is not None


def test_app_compaction_uses_horizon_summarizer():
    summarizer = app.events_compaction_config.summarizer
    assert isinstance(summarizer, HorizonSummarizer)


def test_app_compaction_has_token_threshold_mode():
    config = app.events_compaction_config
    assert config.token_threshold is not None
    assert config.token_threshold > 0
    assert config.event_retention_size is not None
    assert config.event_retention_size > 0


def test_app_compaction_has_sliding_window_mode():
    config = app.events_compaction_config
    assert config.compaction_interval > 0
    assert config.overlap_size >= 0
