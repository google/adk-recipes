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

# No region: everything this root creates is project-scoped and global.
provider "google" {
  project = var.project_id
}

# Ordering only. Service Usage has to be on before the rest are asked for, and
# a provider alias is what gives those two sets separate `depends_on` edges.
#
# It does not change which project the quota is charged to: that follows
# `billing_project` with `user_project_override`, and neither is set. The
# provider also fills an empty `project` from the ambient `GOOGLE_CLOUD_PROJECT`,
# so this alias is not project-less on the machines the README describes.
provider "google" {
  alias = "api_bootstrap"
}
