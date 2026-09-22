resource "google_cloud_run_v2_service" "app" {
  name     = var.service_name
  location = var.region
  project  = var.project_id

  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.cloud_run_deletion_protection
  # Backend is invoked by SA only (web frontend, scheduler, A2A peers);
  # browser auth lives on the lha-web service (see cloud_run_web.tf).
  iap_enabled = false

  template {
    service_account = google_service_account.cloud_run.email

    # Long timeout so an A2A streaming request handler isn't capped at the
    # 300s default while the producer task keeps running after a client
    # disconnect. 3600s is the Cloud Run v2 maximum.
    timeout = "3600s"

    # Pin a client to the instance that owns its in-flight A2A producer task,
    # so a reconnect/resubscribe lands back on the same container.
    session_affinity = true

    # Cap concurrent requests per instance. Bounds the connection footprint: the
    # DB connection budget (db_max_connections) is sized for max_instance_count x
    # this. Kept in sync with `agents-cli deploy --concurrency` so a redeploy and
    # a terraform apply don't fight over the value.
    max_instance_request_concurrency = 8

    scaling {
      # min_instance_count >= 1 so the every-minute tick doesn't pay a
      # cold-start tax, and so the container can't be recycled mid-run
      # when traffic is sparse — the A2A producer task that survives
      # client disconnect runs inside this instance.
      # max_instance_count bounds total DB connections: keep it x the per-instance
      # pool cap under db_max_connections, and in sync with `agents-cli deploy`.
      min_instance_count = 1
      max_instance_count = 8
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.db.connection_name]
      }
    }

    containers {
      image = var.image_uri

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        # Always-on CPU: the A2A SDK keeps a producer asyncio task running
        # after a client disconnects (writing events into the TaskStore for
        # later resubscribe). With cpu_idle = true that task gets starved as
        # soon as Cloud Run considers the request "done".
        cpu_idle = false
        # Extra CPU during container start so the heavy import (Vertex auth,
        # building the Gemini DispatchingLlm, skill catalog load) isn't
        # CPU-starved — cuts cold-start latency before the first request lands.
        startup_cpu_boost = true
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      # A2A agent card: rpc_url and agent_version. Without APP_URL the card
      # falls back to http://0.0.0.0:8000 which breaks remote A2A clients.
      env {
        name  = "APP_URL"
        value = local.scheduler_audience
      }

      env {
        name  = "AGENT_VERSION"
        value = var.agent_version
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "True"
      }

      # Sessions: ADK DatabaseSessionService (async SQLAlchemy via asyncpg).
      env {
        name = "SESSION_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_db_url.secret_id
            version = "latest"
          }
        }
      }

      # A2A tasks: DatabaseTaskStore (async SQLAlchemy via asyncpg).
      env {
        name = "TASK_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.task_db_url.secret_id
            version = "latest"
          }
        }
      }

      # Without this, routines fall back to the in-memory store, which is
      # per-replica: a routine created in one Cloud Run instance is invisible
      # to the /lha/routines reader and the routine-tick on any other instance.
      env {
        name  = "LHA_ROUTINE_STORE"
        value = "postgres"
      }

      env {
        name = "LHA_REMINDER_DB_URL"
        # asyncpg unix-socket DSN; Cloud Run injects a socket at
        # /cloudsql/<connection_name> when the volume mount is attached.
        # Pulled from Secret Manager so the password doesn't land in
        # plain text in Cloud Run env metadata or Terraform state diffs.
        # Name is historical, now owned solely by the routine store; kept
        # as-is to avoid a Terraform/env rename for no user benefit.
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }

      # Shared Agent Engine — both _resolve_sandbox_engine (sandbox host) and
      # _resolve_service_uris (Memory Bank) read this. Provisioned out-of-band
      # via the external data source in agent_engine.tf.
      env {
        name  = "AGENT_ENGINE_RESOURCE_NAME"
        value = local.agent_engine_resource_name
      }

      env {
        name  = "LHA_ENVIRONMENT_BACKEND"
        value = "sandbox"
      }

      env {
        name  = "LHA_RUNTIME_IMAGE"
        value = var.lha_runtime_image
      }

      # Phase C: snapshot-based TTL survival. Activates both restore-on-session-start
      # and the daily /scheduler/snapshot job (snapshot + prune). Capture semantics
      # validated against the live API by scripts/probes/probe_sandbox_snapshot.py.
      env {
        name  = "LHA_SNAPSHOT_ENABLED"
        value = "1"
      }

      # Cloud Run SA doubles as the sandbox caller; the self-grant of
      # serviceAccountTokenCreator (iam.tf) lets it mint its own JWT.
      env {
        name  = "LHA_SANDBOX_CALLER_SA"
        value = google_service_account.cloud_run.email
      }

      env {
        name  = "LHA_SCHEDULER_SA"
        value = google_service_account.scheduler.email
      }

      env {
        name  = "LHA_SCHEDULER_AUDIENCE"
        value = local.scheduler_audience
      }

      # Backend verifies identity itself (iap mode): lha-web forwards the IAP
      # JWT (verified against the lha-web audience) and Gemini Enterprise forwards
      # an end-user OAuth bearer on /a2a. No plaintext X-LHA-User-Id is trusted.
      env {
        name  = "LHA_AUTH_MODE"
        value = "iap"
      }
      env {
        name  = "LHA_IAP_AUDIENCE"
        value = local.lha_web_iap_audience
      }

      # Durable artifact storage (ADK GcsArtifactService). See artifact_bucket.tf.
      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.artifacts.name
      }

      # Connect Google OAuth — only wired when all three vars are set
      # (local.gcp_oauth_enabled in gcp_oauth.tf); otherwise the feature is off.
      dynamic "env" {
        for_each = local.gcp_oauth_enabled ? [1] : []
        content {
          name  = "LHA_GCP_OAUTH_CLIENT_ID"
          value = var.gcp_oauth_client_id
        }
      }
      dynamic "env" {
        for_each = local.gcp_oauth_enabled ? [1] : []
        content {
          name  = "LHA_GCP_OAUTH_REDIRECT_URI"
          value = var.gcp_oauth_redirect_uri
        }
      }
      dynamic "env" {
        for_each = local.gcp_oauth_enabled ? [1] : []
        content {
          name = "LHA_GCP_OAUTH_CLIENT_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.gcp_oauth_client_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_project_iam_member.cloud_run,
    google_service_account_iam_member.cloud_run_token_creator_self,
    google_secret_manager_secret_iam_member.cloud_run_db_password,
    google_secret_manager_secret_iam_member.cloud_run_db_url,
    google_secret_manager_secret_iam_member.cloud_run_session_db_url,
    google_secret_manager_secret_iam_member.cloud_run_task_db_url,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_version.db_password,
    google_secret_manager_secret_version.session_db_url,
    google_secret_manager_secret_version.task_db_url,
    google_secret_manager_secret_version.gcp_oauth_client_secret,
  ]

  # Image rotation is owned by `agents-cli deploy` / `gcloud run deploy`.
  # Terraform sets the initial image via var.image_uri but otherwise stays
  # out of the way so a redeploy doesn't get reverted on the next
  # `terraform apply`. Same for client.* labels Cloud Run stamps itself.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
    ]
  }
}

data "google_project" "current" {
  project_id = var.project_id
}

# Predictable Cloud Run URL format using project number — Cloud Run accepts
# this as a valid hostname for the service. Lets us pin the OIDC audience
# without referencing `google_cloud_run_v2_service.app.uri` (which would
# create a self-reference cycle, since the service consumes the audience
# as a container env var). Cloud Run's IAM check rejects tokens whose `aud`
# doesn't match a real Cloud Run URL, so a fabricated string won't work.
locals {
  scheduler_audience = "https://${var.service_name}-${data.google_project.current.number}.${var.region}.run.app"
}
