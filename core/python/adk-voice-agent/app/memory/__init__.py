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

"""What ADK Voice Agent remembers about each user: one structured profile in Memory Bank.

- `profile`: the `user-profile` schema and the one renderer that turns a profile
  into a delimited data block (voice instruction and thread prompts).
- `store`: the `MemoryStore` protocol, its Vertex AI Memory Bank backend, and
  `recall_profile()`, the one read path (short timeout, nothing on failure).
- `ingest`: the one filter that decides which session events may be remembered.
"""
