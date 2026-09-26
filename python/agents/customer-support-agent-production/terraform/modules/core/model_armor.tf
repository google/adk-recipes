# ==============================================================================
# Model Armor
# ==============================================================================
# Configures project-level floor settings that automatically screen every
# Vertex AI generateContent call — including those made internally by
# Agent Engine — for harmful content, prompt injection, and jailbreaks.
#
# No changes to agents or tools are required. Protection is applied
# transparently at the Gemini API layer.
#
# Requires: google provider >= 6.14.0
#           var.model_armor_enabled = true (default)
#
# IAM grants (roles/modelarmor.user on Vertex AI SAs) are in iam.tf.
# API enablement (modelarmor.googleapis.com) is in apis.tf.

# ==============================================================================
# DLP templates for SDP (Sensitive Data Protection) filter in the template below
# Screens for PII that should not appear in customer support conversations
# ==============================================================================

resource "google_data_loss_prevention_inspect_template" "pii_inspector" {
  count  = var.model_armor_enabled ? 1 : 0
  parent = "projects/${var.project_id}/locations/${var.region}"

  display_name = "Customer Support PII Inspector"
  template_id  = "customer-support-pii-inspector"

  inspect_config {
    info_types { name = "CREDIT_CARD_NUMBER" }
    info_types { name = "US_SOCIAL_SECURITY_NUMBER" }
    info_types { name = "EMAIL_ADDRESS" }
    info_types { name = "STREET_ADDRESS" }
    info_types { name = "GCP_API_KEY" }
  }

  depends_on = [google_project_service.apis]
}

resource "google_data_loss_prevention_deidentify_template" "pii_redactor" {
  count  = var.model_armor_enabled ? 1 : 0
  parent = "projects/${var.project_id}/locations/${var.region}"

  display_name = "Customer Support PII Redactor"
  template_id  = "customer-support-pii-redactor"

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_config {
            new_value {
              string_value = "[redacted]"
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# ==============================================================================
# Model Armor template — per-deployment policy used by the ADK plugin and backend
# Template resource name is emitted as an output for use in .env / Cloud Run env
# ==============================================================================

resource "google_model_armor_template" "customer_support_policy" {
  count       = var.model_armor_enabled ? 1 : 0
  template_id = "customer-support-policy"
  location    = var.region
  project     = var.project_id

  filter_config {
    # MEDIUM_AND_ABOVE: customer support queries (e.g. "Track order ORD-12345",
    # "Cancel my subscription") look like commands and produce false positives at
    # LOW confidence. Floor settings retain LOW_AND_ABOVE for project-wide safety.
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }

    rai_settings {
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "LOW_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    sdp_settings {
      advanced_config {
        inspect_template    = google_data_loss_prevention_inspect_template.pii_inspector[0].id
        deidentify_template = google_data_loss_prevention_deidentify_template.pii_redactor[0].id
      }
    }
  }

  template_metadata {
    log_template_operations = true
  }

  depends_on = [
    google_project_service.apis,
    google_model_armor_floorsetting.default,
  ]
}

# ==============================================================================
# Model Armor floor settings (project-level — applies to all Gemini calls)
# ==============================================================================

resource "google_model_armor_floorsetting" "default" {
  count    = var.model_armor_enabled ? 1 : 0
  parent   = "projects/${var.project_id}"
  location = "global"

  # Reject requests and responses that violate thresholds.
  # Set to false (or use INSPECT_ONLY in filter_config) to log-only.
  enable_floor_setting_enforcement = var.model_armor_floor_mode == "INSPECT_AND_BLOCK"

  filter_config {
    # MEDIUM_AND_ABOVE: floor must match or be stricter than template.
    # LOW_AND_ABOVE caused false positives on customer support queries like
    # "Track order ORD-12345" (flagged as prompt injection at low confidence).
    rai_settings {
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "LOW_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }

    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
  }

  depends_on = [google_project_service.apis]
}
