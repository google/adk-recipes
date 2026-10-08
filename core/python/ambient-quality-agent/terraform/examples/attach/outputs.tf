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

output "scheduler_job" {
  description = "Name of the Cloud Scheduler job that runs the periodic investigation."
  value       = google_cloud_scheduler_job.tick.name
}

output "update_sink" {
  description = "Resource name of the audit log sink on the observed engine's updates, as `projects/<project>/sinks/<name>`, or null when none is watched."
  value       = try(google_logging_project_sink.updates[0].id, null)
}

output "instance" {
  description = <<-EOT
    The instance variables this state was applied with. A destroy takes them
    from here, so it removes the triggers that call this instance and no
    other.
  EOT
  value = {
    region               = var.region
    aqua_engine          = var.aqua_engine
    ambient_topic        = var.ambient_topic
    ambient_caller_email = var.ambient_caller_email
  }
}
