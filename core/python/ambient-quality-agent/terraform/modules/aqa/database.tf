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

# AQuA's own database when `storage_backend` is `cloudsql`. The engine reaches
# it through `network.tf` and logs in as its service account; there are no
# passwords.

resource "google_sql_database_instance" "aqa" {
  count = local.cloudsql ? 1 : 0

  # The instance gets its private IP from the peered range.
  depends_on = [google_service_networking_connection.cloudsql]

  project             = var.project_id
  name                = local.prefix
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = !var.force_destroy

  settings {
    # PostgreSQL 16 and later default to Enterprise Plus, which has no
    # shared-core tiers.
    edition           = "ENTERPRISE"
    tier              = var.cloudsql_tier
    availability_type = var.cloudsql_availability
    user_labels       = local.common_labels

    deletion_protection_enabled = !var.force_destroy

    ip_configuration {
      ipv4_enabled    = false
      private_network = local.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }
}

# `ABANDON` on the database and the user: Postgres refuses to drop a role that
# owns objects, and the engine's user owns the `aqa` schema once the engine
# has migrated it. Deleting the instance removes both.
resource "google_sql_database" "aqa" {
  count = local.cloudsql ? 1 : 0

  project         = var.project_id
  instance        = google_sql_database_instance.aqa[0].name
  name            = "aqa"
  deletion_policy = "ABANDON"
}

# `cloudsqlsuperuser` lets the engine create the `aqa` schema, which it then
# owns, so migrations need no separate admin step.
resource "google_sql_user" "engine" {
  count = local.cloudsql ? 1 : 0

  project         = var.project_id
  instance        = google_sql_database_instance.aqa[0].name
  name            = trimsuffix(google_service_account.engine.email, ".gserviceaccount.com")
  type            = "CLOUD_IAM_SERVICE_ACCOUNT"
  database_roles  = ["cloudsqlsuperuser"]
  deletion_policy = "ABANDON"
}

locals {
  cloudsql_instance_condition = local.cloudsql ? {
    title      = "${local.prefix} database"
    expression = "resource.name == \"projects/${var.project_id}/instances/${google_sql_database_instance.aqa[0].name}\""
  } : null
}

# Connecting through the Cloud SQL connector, which asks the Admin API for the
# instance's address and a client certificate.
resource "google_project_iam_member" "engine_cloudsql_client" {
  count = local.cloudsql ? 1 : 0

  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = local.engine_member

  condition {
    title      = local.cloudsql_instance_condition.title
    expression = local.cloudsql_instance_condition.expression
  }
}

# Logging in to the instance as the IAM user above.
resource "google_project_iam_member" "engine_cloudsql_instance_user" {
  count = local.cloudsql ? 1 : 0

  project = var.project_id
  role    = "roles/cloudsql.instanceUser"
  member  = local.engine_member

  condition {
    title      = local.cloudsql_instance_condition.title
    expression = local.cloudsql_instance_condition.expression
  }
}
