resource "google_cloud_scheduler_job" "routine_tick" {
  name        = "${var.service_name}-routine-tick"
  description = "Fires every minute to run due routines."
  schedule    = "* * * * *"
  region      = var.region
  project     = var.project_id
  time_zone   = "Etc/UTC"

  # A routine turn provisions a fresh isolated sandbox and runs a full agent
  # turn, so it needs far longer than a simple flush. Overlapping ticks are
  # safe: claim_due advances next_fire_at atomically, so no double-fire.
  attempt_deadline = "1800s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/scheduler/routine-tick"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.scheduler_audience
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}

resource "google_cloud_scheduler_job" "sandbox_snapshot" {
  name        = "${var.service_name}-sandbox-snapshot"
  description = "Daily per-user sandbox snapshot + prune (TTL survival)."
  schedule    = "37 3 * * *"
  region      = var.region
  project     = var.project_id
  time_zone   = "Etc/UTC"

  attempt_deadline = "1800s"

  retry_config {
    retry_count          = 2
    min_backoff_duration = "60s"
    max_backoff_duration = "600s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/scheduler/snapshot"
    headers     = { "Content-Type" = "application/json" }
    body = base64encode(jsonencode({
      user_ids = var.snapshot_user_ids
      app_name = var.agent_app_name
    }))

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.scheduler_audience
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}

resource "google_cloud_scheduler_job" "dream_review" {
  name        = "${var.service_name}-dream-review"
  description = "Daily memory consolidation pass."
  schedule    = "0 4 * * *"
  region      = var.region
  project     = var.project_id
  time_zone   = "Etc/UTC"

  attempt_deadline = "1800s"

  # Dream-review runs once a day — a transient Cloud Run blip would
  # otherwise skip the whole day. Bounded retries give one or two
  # second-chances without compounding load.
  retry_config {
    retry_count          = 2
    min_backoff_duration = "60s"
    max_backoff_duration = "600s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/scheduler/dream-review"
    headers     = { "Content-Type" = "application/json" }
    body = base64encode(jsonencode({
      user_ids = var.dream_review_user_ids
      app_name = var.agent_app_name
    }))

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.scheduler_audience
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}
