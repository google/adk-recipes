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

# =============================================================================
# Media bucket (user uploads & AI outputs)
# =============================================================================

resource "google_storage_bucket" "media" {
  name     = "${var.project_id}-genmedia-for-commerce-media-payloads"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Run folders (inputs saved with GENMEDIA_INPUT=gcs or POST /api/uploads,
  # results saved with GENMEDIA_OUTPUT=gcs) are temporary.
  lifecycle_rule {
    condition {
      age            = var.media_retention_days
      matches_prefix = ["runs/"]
    }
    action {
      type = "Delete"
    }
  }

  # Chat history, Studio state and Studio jobs (sessions/) are deleted after
  # the same number of days without a change.
  lifecycle_rule {
    condition {
      age            = var.media_retention_days
      matches_prefix = ["sessions/"]
    }
    action {
      type = "Delete"
    }
  }

  # The photo try-on's preprocessing cache (GENMEDIA_USE_CACHE): what came
  # from product photos (product_cache/), deleted after the same number of
  # days. Nothing writes user_cache/ anymore: it stays so that the person
  # photo results older versions kept there are deleted too.
  lifecycle_rule {
    condition {
      age            = var.media_retention_days
      matches_prefix = ["user_cache/", "product_cache/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}
