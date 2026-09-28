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

"""Shared constants used across multiple horizon modules."""

from __future__ import annotations

APP_NAME = "app"

# Session-source tagging for scheduler-created sessions. Read by
# app/api/sessions.py (to filter/group) and written by
# app/scheduler/sessions.py. "scheduler" marks a cron/scheduled session;
# its absence means a normal user chat.
SESSION_SOURCE_KEY = "system:session_source"
SCHEDULER_JOB_KEY = "system:scheduler_job"
SCHEDULER_SOURCE = "scheduler"

# Sidebar chat title. Set by the rename endpoint and stamped from the first user
# message at session start; read by app/api/sessions.py to render the
# chat history list without scanning event history.
TITLE_KEY = "lha_chat_title"
TITLE_MAX_LEN = 80
