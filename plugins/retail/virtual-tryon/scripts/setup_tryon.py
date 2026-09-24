#!/usr/bin/env python3
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

"""Set up GCP resources for virtual try-on.

Creates GCS buckets for user photo uploads and output images,
verifies that Gemini image models and Veo models are accessible,
provisions/uploads the shop catalog to GCS,
and prepares the local sample catalog for testing.
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from google.cloud import storage

# Add parent of scripts directory to path to enable package resolution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _design_spec_to_env(design_spec_path: str) -> None:
    """Bridge design-spec.md keys into environment variables.

    Q-MODE writes user answers into design-spec.md. This function reads the
    YAML frontmatter and calls os.environ.setdefault() for each recognized
    key so that scripts.config picks them up. setdefault() is used so any
    values the caller already exported win.
    """
    if not Path(design_spec_path).exists():
        return
    cfg = load_config(design_spec_path)
    if not cfg:
        return
    project_id = cfg.get("gcp_project_id")
    if project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
    if cfg.get("gcp_region"):
        os.environ.setdefault("GCP_REGION", cfg["gcp_region"])
    if cfg.get("tryon_model"):
        os.environ.setdefault("GEMINI_IMAGE_MODEL", cfg["tryon_model"])
    output_bucket = cfg.get("tryon_output_bucket") or (
        f"{project_id}-tryon-output" if project_id else None
    )
    if output_bucket:
        os.environ.setdefault("TRYON_OUTPUT_BUCKET", output_bucket)
    upload_bucket = cfg.get("tryon_upload_bucket") or (
        f"{project_id}-tryon-uploads" if project_id else None
    )
    if upload_bucket:
        os.environ.setdefault("TRYON_UPLOAD_BUCKET", upload_bucket)
    if cfg.get("tryon_catalog_path"):
        os.environ.setdefault("TRYON_CATALOG_PATH", cfg["tryon_catalog_path"])


# Models mapping for verification
IMAGE_MODELS = {
    "gemini-2.5-flash-image": "gemini-2.5-flash-image",
    "gemini-2.5-pro-image": "gemini-2.5-pro-image",
    "flash": "gemini-2.5-flash-image",
    "pro": "gemini-2.5-pro-image",
}

VIDEO_MODELS = {"veo": "veo-3.1-generate-001"}


def load_config(config_path: str) -> dict:
    """Load config from YAML frontmatter of design-spec.md."""
    import re

    import yaml

    try:
        content = Path(config_path).read_text()
        parts = re.split(r"^---\s*$", content, flags=re.MULTILINE)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}
    except Exception as e:
        logger.warning(f"Failed to load config from {config_path}: {e}")
    return {}


def create_bucket_if_needed(
    project_id: str,
    bucket_name: str,
    location: str | None = None,
    auto_delete_days: int = 0,
) -> None:
    """Create a GCS bucket if it doesn't exist."""
    effective_location = location or config.GCP_REGION
    client = storage.Client(project=project_id)
    try:
        client.get_bucket(bucket_name)
        logger.info(f"Bucket {bucket_name} already exists.")
    except Exception:
        logger.info(
            f"Creating bucket: {bucket_name} in location {effective_location}..."
        )
        bucket = client.create_bucket(bucket_name, location=effective_location)
        if auto_delete_days > 0:
            bucket.add_lifecycle_delete_rule(age=auto_delete_days)
            bucket.patch()
            logger.info(
                f"Created bucket {bucket_name} with {auto_delete_days}-day auto-delete lifecycle rule."
            )
        else:
            logger.info(f"Created bucket {bucket_name}.")


def resolve_model_id(model_label: str) -> str:
    """Resolve a label (flash/pro) or a full model ID."""
    if model_label in IMAGE_MODELS:
        return IMAGE_MODELS[model_label]
    if model_label in IMAGE_MODELS.values():
        return model_label
    return IMAGE_MODELS["flash"]


def enable_required_apis(project_id: str) -> None:
    """Enable all required GCP services."""
    apis = [
        "run.googleapis.com",
        "cloudbuild.googleapis.com",
        "aiplatform.googleapis.com",
        "storage.googleapis.com",
    ]
    logger.info(f"Enabling required GCP APIs: {', '.join(apis)} ...")
    try:
        subprocess.run(
            [
                "gcloud",
                "services",
                "enable",
                *apis,
                "--project",
                project_id,
                "--quiet",
            ],
            capture_output=True,
            check=True,
        )
        logger.info("Required APIs enabled successfully.")
    except Exception as e:
        logger.warning(f"Could not enable required APIs: {e}")


def setup_service_account_permissions(project_id: str) -> None:
    """Ensure Default Compute Service Account has required Vertex AI and Storage permissions."""
    try:
        res = subprocess.run(
            [
                "gcloud",
                "projects",
                "describe",
                project_id,
                "--format=value(projectNumber)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        project_number = res.stdout.strip()
        sa_email = f"{project_number}-compute@developer.gserviceaccount.com"

        for role in ["roles/aiplatform.user", "roles/storage.objectAdmin"]:
            logger.info(
                f"Granting IAM role '{role}' to default compute service account {sa_email} ..."
            )
            subprocess.run(
                [
                    "gcloud",
                    "projects",
                    "add-iam-policy-binding",
                    project_id,
                    f"--member=serviceAccount:{sa_email}",
                    f"--role={role}",
                    "--quiet",
                ],
                capture_output=True,
                check=True,
            )
        logger.info("IAM permissions configured successfully.")
    except Exception as e:
        logger.warning(
            f"Could not auto-configure IAM permissions for compute service account: {e}"
        )


def verify_api_access(
    project_id: str, model_id: str, location: str | None = None
) -> bool:
    """Verify that a model API is accessible."""
    effective_location = location or config.GCP_REGION
    resolved_location = "global" if "gemini" in model_id else effective_location
    try:
        from google import genai

        client = genai.Client(
            vertexai=True, project=project_id, location=resolved_location
        )
        client.models.get(model=model_id)
        logger.info(
            f"API accessibility verified for model: {model_id} in location {resolved_location}"
        )
        return True
    except Exception as e:
        logger.error(
            f"API verification failed for model {model_id} in location {resolved_location}: {e}"
        )
        logger.error(
            "Please ensure you have Vertex AI enabled at: https://console.cloud.google.com/apis/library/aiplatform.googleapis.com"
        )
        return False


def setup_sample_catalog(catalog_path_str: str = "catalog_images"):
    """Create a local sample catalog folder with mock products for easy testing."""
    catalog_dir = Path(catalog_path_str)
    catalog_dir.mkdir(exist_ok=True)

    # Try copying pre-existing assets from the skill's assets directory first
    assets_src = Path(__file__).parent.parent / "assets" / "catalog_images"
    if assets_src.exists():
        import shutil

        logger.info(f"Copying pre-existing catalog assets from {assets_src}...")
        for item in assets_src.iterdir():
            if item.is_file():
                shutil.copy(item, catalog_dir / item.name)
        logger.info("Local sample catalog setup complete (copied from assets).")
        return

    # Fallback: create dynamic placeholder images if assets directory is not found
    try:
        from PIL import Image, ImageDraw

        logger.info(
            "Assets directory not found. Generating mock placeholder images..."
        )

        # 1. Shirt product image
        shirt_img = Image.new("RGB", (300, 400), (255, 230, 230))
        draw = ImageDraw.Draw(shirt_img)
        draw.rectangle(
            [50, 50, 250, 350], fill=(220, 50, 50), outline=(0, 0, 0)
        )
        draw.text((100, 180), "Red Shirt", fill=(255, 255, 255))
        shirt_img.save(catalog_dir / "shirt_001.jpg", "JPEG")

        # 2. Sunglasses product image
        glasses_img = Image.new("RGB", (300, 200), (230, 255, 230))
        draw = ImageDraw.Draw(glasses_img)
        draw.ellipse([50, 60, 130, 140], fill=(50, 50, 50))
        draw.ellipse([170, 60, 250, 140], fill=(50, 50, 50))
        draw.line([130, 100, 170, 100], fill=(50, 50, 50), width=5)
        glasses_img.save(catalog_dir / "sunglasses_001.jpg", "JPEG")

        # 3. Sample User/Person image
        person_img = Image.new("RGB", (400, 500), (240, 240, 240))
        draw = ImageDraw.Draw(person_img)
        # Face
        draw.ellipse(
            [120, 80, 280, 240], fill=(255, 220, 180), outline=(0, 0, 0)
        )
        # Body/Shoulders
        draw.polygon(
            [(60, 500), (340, 500), (250, 320), (150, 320)], fill=(50, 50, 200)
        )
        draw.text((160, 150), "User Photo", fill=(0, 0, 0))
        person_img.save(catalog_dir / "sample_user.jpg", "JPEG")

        logger.info(
            "Local sample catalog setup complete (generated placeholders)."
        )
    except ImportError:
        logger.warning(
            "PIL not installed and assets not found. Skipping local sample image creation."
        )


def upload_local_catalog_to_gcs(
    project_id: str,
    local_dir_path: str,
    bucket_name: str,
    location: str | None = None,
) -> str:
    """Create a GCS catalog bucket and upload all local images in the folder to it."""
    create_bucket_if_needed(project_id, bucket_name, location)

    gcs_client = storage.Client(project=project_id)
    bucket = gcs_client.bucket(bucket_name)

    local_dir = Path(local_dir_path)
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}

    logger.info(
        f"Uploading local catalog images from {local_dir_path} to GCS bucket gs://{bucket_name}..."
    )
    for p in local_dir.iterdir():
        if p.is_file() and p.suffix.lower() in image_extensions:
            # Skip user photos
            if "user" in p.name.lower() or "person" in p.name.lower():
                continue
            blob = bucket.blob(p.name)
            logger.info(f"  Uploading {p.name} ...")
            blob.upload_from_filename(p)

    return f"gs://{bucket_name}"


def update_design_spec_catalog_path(config_path: str, gcs_uri: str) -> None:
    """Update tryon_catalog_path key inside design-spec.md frontmatter."""
    import re

    try:
        path = Path(config_path)
        if not path.exists():
            return
        content = path.read_text()
        parts = re.split(r"^---\s*$", content, flags=re.MULTILINE)
        if len(parts) >= 3:
            import yaml

            fm = yaml.safe_load(parts[1]) or {}
            fm["tryon_catalog_path"] = gcs_uri
            new_fm_text = yaml.safe_dump(fm, default_flow_style=False)
            parts[1] = "\n" + new_fm_text
            new_content = "---".join(parts)
            path.write_text(new_content)
            logger.info(
                f"Updated tryon_catalog_path to {gcs_uri} inside {config_path}"
            )
    except Exception as e:
        logger.warning(f"Failed to update {config_path} frontmatter: {e}")


def setup(
    project_id: str,
    location: str,
    output_bucket: str,
    upload_bucket: str,
    image_model_label: str,
    enable_video: bool,
    catalog_path: str = "catalog_images",
    config_path: str | None = None,
    gcs_catalog_bucket: str | None = None,
    catalog_upload: bool = True,
) -> bool:
    """Set up all virtual try-on resources."""
    logger.info("=" * 60)
    logger.info(f"Setting up Virtual Try-On for Project: {project_id}")
    logger.info("=" * 60)

    # 0. Enable required APIs and set up Service Account permissions
    enable_required_apis(project_id)
    setup_service_account_permissions(project_id)

    ok = True

    # 1. Create buckets
    if output_bucket:
        create_bucket_if_needed(project_id, output_bucket, location)
    if upload_bucket:
        create_bucket_if_needed(
            project_id, upload_bucket, location, auto_delete_days=1
        )

    # 2. Verify model APIs
    img_model = IMAGE_MODELS.get(image_model_label, IMAGE_MODELS["flash"])
    ok = verify_api_access(project_id, img_model, location) and ok

    if enable_video:
        vid_model = VIDEO_MODELS["veo"]
        ok = verify_api_access(project_id, vid_model, location) and ok

    # 3. Catalog setup
    if catalog_path.startswith("gs://"):
        bucket_name = catalog_path[5:].split("/", 1)[0]
        logger.info(f"Verifying catalog GCS bucket: {bucket_name}...")
        try:
            create_bucket_if_needed(project_id, bucket_name, location)
            logger.info(
                f"Access verified for catalog GCS bucket: {bucket_name}"
            )
        except Exception as e:
            logger.error(f"Failed to verify/create catalog GCS bucket: {e}")
            ok = False
    else:
        # If catalog is local, prepare it, then upload it to GCS if requested
        setup_sample_catalog(catalog_path)

        if catalog_upload:
            catalog_bucket_name = (
                gcs_catalog_bucket or f"{project_id}-tryon-catalog"
            )
            try:
                gcs_catalog_uri = upload_local_catalog_to_gcs(
                    project_id=project_id,
                    local_dir_path=catalog_path,
                    bucket_name=catalog_bucket_name,
                    location=location,
                )
                # Update configuration path to the GCS path so the UI/Server runs in GCS-first mode
                logger.info(
                    f"Local catalog successfully uploaded to: {gcs_catalog_uri}"
                )
                if config_path:
                    update_design_spec_catalog_path(
                        config_path, gcs_catalog_uri
                    )
            except Exception as e:
                logger.error(f"Failed to upload local catalog to GCS: {e}")
                ok = False
        else:
            logger.info(
                "Skipping GCS catalog sync as requested. Catalog will run locally."
            )

    logger.info("")
    logger.info("Setup process finished.")
    if ok:
        logger.info("ALL VERIFICATIONS AND UPLOADS PASSED SUCCESSFULLY.")
    else:
        logger.warning("SOME VERIFICATIONS OR SERVICE ACTIVATIONS FAILED.")
    logger.info("=" * 60)
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="Set up GCP resources for virtual try-on"
    )
    parser.add_argument(
        "--config",
        default="assets/design-spec.md",
        help="Path to design-spec.md",
    )
    parser.add_argument(
        "--project-id", help="Override GOOGLE_CLOUD_PROJECT for this run"
    )
    parser.add_argument("--location", help="Override GCP_REGION for this run")
    parser.add_argument(
        "--output-bucket", help="Override TRYON_OUTPUT_BUCKET for this run"
    )
    parser.add_argument(
        "--upload-bucket", help="Override TRYON_UPLOAD_BUCKET for this run"
    )
    parser.add_argument(
        "--model",
        help="Override GEMINI_IMAGE_MODEL for this run "
        "(flash | pro | full model id)",
    )
    parser.add_argument(
        "--video", action="store_true", help="Enable video try-on (Veo)"
    )
    parser.add_argument(
        "--catalog",
        help="Override TRYON_CATALOG_PATH for this run",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Do not upload local catalog to GCS catalog bucket",
    )
    args = parser.parse_args()

    # Config resolution order (highest priority wins):
    #   1. CLI flags (below)
    #   2. Ambient os.environ / .env (already loaded by scripts.config)
    #   3. design-spec.md keys, bridged via _design_spec_to_env
    #   4. Hardcoded fallbacks in scripts.config
    _design_spec_to_env(args.config)

    # CLI flags override everything else.
    if args.project_id:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project_id
    if args.location:
        os.environ["GCP_REGION"] = args.location
    if args.output_bucket:
        os.environ["TRYON_OUTPUT_BUCKET"] = args.output_bucket
    if args.upload_bucket:
        os.environ["TRYON_UPLOAD_BUCKET"] = args.upload_bucket
    if args.model:
        os.environ["GEMINI_IMAGE_MODEL"] = args.model
    if args.catalog:
        os.environ["TRYON_CATALOG_PATH"] = args.catalog

    # Now every value has one source of truth: config.*.
    project_id = config.GOOGLE_CLOUD_PROJECT
    if not project_id:
        logger.error(
            "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env, run "
            "Q-MODE to populate design-spec.md, or pass --project-id."
        )
        sys.exit(1)

    output_bucket = config.TRYON_OUTPUT_BUCKET or f"{project_id}-tryon-output"
    upload_bucket = config.TRYON_UPLOAD_BUCKET or f"{project_id}-tryon-uploads"

    # Non-env config (mode, catalog upload flag, GCS catalog bucket) is
    # design-spec.md-only for now — those are Q-MODE artifacts, not
    # runtime knobs.
    cfg = load_config(args.config) if Path(args.config).exists() else {}
    enable_video = args.video or (
        cfg.get("tryon_mode") in ("image_and_video", "video")
    )
    catalog = config.TRYON_CATALOG_PATH
    if catalog.lower() == "demo":
        catalog = "catalog_images"
    catalog_bucket = cfg.get("gcs_catalog_bucket")
    catalog_upload = not args.no_upload
    if cfg.get("tryon_catalog_upload") is False:
        catalog_upload = False

    success = setup(
        project_id=project_id,
        location=config.GCP_REGION,
        output_bucket=output_bucket,
        upload_bucket=upload_bucket,
        image_model_label=config.GEMINI_IMAGE_MODEL,
        enable_video=enable_video,
        catalog_path=catalog,
        config_path=args.config,
        gcs_catalog_bucket=catalog_bucket,
        catalog_upload=catalog_upload,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
