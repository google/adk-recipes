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

# The Agent Engine that holds each user's memories (Memory Bank). It runs no code:
# sessions live in the app's database, and the engine does one job.
#
# Its Memory Bank settings (the user-profile schema) are defined by the app's code
# (app/memory/store.py) and registered by `make deploy` after each apply, with your
# credentials (scripts/deploy.py). The app's own account only reads and writes
# memories (roles/aiplatform.user, iam.tf), which can't change the engine.
resource "google_vertex_ai_reasoning_engine" "memory" {
  project      = var.project_id
  region       = var.region
  display_name = var.service_name
  description  = "Memory Bank for ${var.service_name}: each user's profile and recaps of their conversations and threads."

  # `make destroy` deletes everything this deployment made, memories included.
  deletion_policy = "FORCE"

  depends_on = [google_project_service.enabled]
}
