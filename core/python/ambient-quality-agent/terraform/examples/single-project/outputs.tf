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

output "buckets" {
  description = "Bucket names, keyed by purpose. Names, not `gs://` URLs."
  value       = module.aqa.buckets
}

output "ui_service_name" {
  description = "Cloud Run service name of the Insights UI, which `agents-cli deploy` pushes the dashboard image onto."
  value       = module.aqa.ui_service_name
}

output "ui_service_uri" {
  description = "URL of the Insights UI service. Private: calling it requires auth."
  value       = module.aqa.ui_service_uri
}
