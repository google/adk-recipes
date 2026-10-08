# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Named objects AQuA keeps outside its dataset: documents, agent configs, source.

Wiring::

    documents.goal, documents.memories, agent_config
        │ take an ObjectStore
        ▼
    store.jobs_store_factory() ◄── bound by backends.configure_providers()
        │
        ├── deployed ───► gcs_store.GcsObjectStore      gs://<jobs bucket>/<name>
        └── standalone ─► standalone.files.FileObjectStore  .aqua/job/<name>

    source_code.reader, source_code.upload
        │ take an ObjectStore
        ▼
    store.source_store_factory() ◄── bound by backends.configure_providers()
        │
        ├── deployed ───► gcs_store.GcsObjectStore      gs://<source bucket>/<name>
        └── standalone ─► standalone.files.FileObjectStore  .aqua/source/<name>

`store.ObjectStore` is the contract; the two implementations keep the same
names under a bucket and under a directory.
"""
