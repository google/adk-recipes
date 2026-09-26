"""
Creates the Model Armor template for the customer support app.

Used as a fallback when `gcloud model-armor` is not available
(requires gcloud SDK >= 498 or so).

Usage:
    python ops/create_model_armor_template.py
    python ops/create_model_armor_template.py --template-id my-template

Outputs the full resource name to stdout so it can be set in .env:
    MODEL_ARMOR_TEMPLATE_ID=projects/.../locations/.../templates/...
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

if not PROJECT_ID:
    print("ERROR: GOOGLE_CLOUD_PROJECT not set")
    sys.exit(1)

try:
    from google.api_core.client_options import ClientOptions
    from google.cloud import modelarmor_v1
except ImportError:
    print("ERROR: google-cloud-modelarmor not installed")
    print("Run:  uv add google-cloud-modelarmor")
    sys.exit(1)

parser = argparse.ArgumentParser()
parser.add_argument("--template-id", default="customer-support-policy")
args = parser.parse_args()

TEMPLATE_ID = args.template_id

client = modelarmor_v1.ModelArmorClient(
    client_options=ClientOptions(api_endpoint=f"modelarmor.{LOCATION}.rep.googleapis.com")
)

template = modelarmor_v1.Template(
    filter_config=modelarmor_v1.FilterConfig(
        pi_and_jailbreak_filter_settings=modelarmor_v1.PiAndJailbreakFilterSettings(
            filter_enforcement=modelarmor_v1.PiAndJailbreakFilterSettings.PiAndJailbreakFilterEnforcement.ENABLED,
            confidence_level=modelarmor_v1.DetectionConfidenceLevel.LOW_AND_ABOVE,
        ),
        malicious_uri_filter_settings=modelarmor_v1.MaliciousUriFilterSettings(
            filter_enforcement=modelarmor_v1.MaliciousUriFilterSettings.MaliciousUriFilterEnforcement.ENABLED,
        ),
        rai_settings=modelarmor_v1.RaiFilterSettings(
            rai_filters=[
                modelarmor_v1.RaiFilterSettings.RaiFilter(
                    filter_type=modelarmor_v1.RaiFilterType.HARASSMENT,
                    confidence_level=modelarmor_v1.DetectionConfidenceLevel.LOW_AND_ABOVE,
                ),
                modelarmor_v1.RaiFilterSettings.RaiFilter(
                    filter_type=modelarmor_v1.RaiFilterType.HATE_SPEECH,
                    confidence_level=modelarmor_v1.DetectionConfidenceLevel.MEDIUM_AND_ABOVE,
                ),
                modelarmor_v1.RaiFilterSettings.RaiFilter(
                    filter_type=modelarmor_v1.RaiFilterType.DANGEROUS,
                    confidence_level=modelarmor_v1.DetectionConfidenceLevel.LOW_AND_ABOVE,
                ),
            ]
        ),
    ),
    template_metadata=modelarmor_v1.Template.TemplateMetadata(
        log_template_operations=True,
    ),
)

parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"

print(f"Creating template '{TEMPLATE_ID}' in {parent} ...")

try:
    result = client.create_template(
        request=modelarmor_v1.CreateTemplateRequest(
            parent=parent,
            template_id=TEMPLATE_ID,
            template=template,
        )
    )
    print("\nTemplate created:")
    print(f"  {result.name}")
    print("\nAdd to .env:")
    print("  MODEL_ARMOR_ENABLED=true")
    print(f"  MODEL_ARMOR_TEMPLATE_ID={result.name}")

except Exception as e:
    if "already exists" in str(e).lower() or "ALREADY_EXISTS" in str(e):
        resource_name = f"{parent}/templates/{TEMPLATE_ID}"
        print("\nTemplate already exists:")
        print(f"  {resource_name}")
        print("\nAdd to .env:")
        print("  MODEL_ARMOR_ENABLED=true")
        print(f"  MODEL_ARMOR_TEMPLATE_ID={resource_name}")
    else:
        print(f"ERROR: {e}")
        sys.exit(1)
