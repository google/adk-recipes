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

# Optional durable thread store (enable_cloud_sql). The app reads THREAD_DB_URL, mounted
# from Secret Manager; Cloud Run reaches the instance over the /cloudsql socket.

resource "google_sql_database_instance" "db" {
  count = var.enable_cloud_sql ? 1 : 0

  name                = "${var.service_name}-db"
  database_version    = "POSTGRES_16"
  region              = var.region
  project             = var.project_id
  deletion_protection = var.db_deletion_protection

  settings {
    # Shared-core tiers like db-f1-micro exist only in the Enterprise edition; newer
    # Postgres versions otherwise default to Enterprise Plus, which rejects them.
    edition         = "ENTERPRISE"
    tier            = var.db_tier
    disk_size       = 10
    disk_autoresize = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    final_backup_config {
      enabled = true
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "voice_agent" {
  count = var.enable_cloud_sql ? 1 : 0

  name     = "voice_agent"
  instance = google_sql_database_instance.db[0].name
  project  = var.project_id
}

resource "random_password" "db" {
  count = var.enable_cloud_sql ? 1 : 0

  length  = 32
  special = false
}

resource "google_sql_user" "voice_agent" {
  count = var.enable_cloud_sql ? 1 : 0

  name     = "voice_agent"
  instance = google_sql_database_instance.db[0].name
  password = random_password.db[0].result
  project  = var.project_id

  # Postgres won't drop a role that owns tables; deleting the instance removes it.
  deletion_policy = "ABANDON"
}

locals {
  thread_db_secret_id = "${var.service_name}-thread-db-url"
}

resource "google_secret_manager_secret" "thread_db_url" {
  count = var.enable_cloud_sql ? 1 : 0

  secret_id = local.thread_db_secret_id
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "thread_db_url" {
  count = var.enable_cloud_sql ? 1 : 0

  secret      = google_secret_manager_secret.thread_db_url[0].id
  secret_data = "postgresql+asyncpg://${google_sql_user.voice_agent[0].name}:${random_password.db[0].result}@/${google_sql_database.voice_agent[0].name}?host=/cloudsql/${google_sql_database_instance.db[0].connection_name}"
}

resource "google_secret_manager_secret_iam_member" "thread_db_url" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.thread_db_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}
