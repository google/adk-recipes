# Copyright 2025 Google LLC
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

"""Image analysis and management tools using Gemini."""

import json
import os

from google import genai
from google.genai import types

from .. import storage
from ..config import GCS_BUCKET, SELECTED_DIR
from ..database.db import get_db_cursor


def analyze_image(image_filename: str) -> dict:
    """Analyze a fashion image using Gemini to extract metadata.

    Analyzes the image to extract:
    - Model characteristics (gender, hair color, etc.)
    - Setting (outdoor, studio, urban, etc.)
    - Clothing details (color, style, pattern)
    - Mood/atmosphere

    This metadata is used for generating video prompts.

    Args:
        image_filename: Filename of image in the selected/ folder

    Returns:
        Dictionary with structured metadata for video prompt generation
    """
    # Use storage module for both local and GCS mode
    if not storage.image_exists(image_filename):
        return {
            "status": "error",
            "message": f"Image not found: {image_filename}",
        }

    try:
        client = genai.Client()

        # Read image file using storage abstraction
        image_bytes = storage.read_image(image_filename)

        image_part = types.Part.from_bytes(
            data=image_bytes, mime_type="image/jpeg"
        )

        prompt = """Analyze this fashion image and extract the following metadata in JSON format:

{
    "model_description": "Brief description of the model (e.g., 'a woman with blonde hair')",
    "clothing_description": "Detailed description of the main garment (e.g., 'a flowing floral wrap dress in pink and white')",
    "setting_description": "Description of the setting/background (e.g., 'In a sun-drenched meadow')",
    "garment_type": "Type of garment (e.g., 'summer dress', 'blazer', 'turtleneck')",
    "movement": "Suggested movement for video (e.g., 'billows gracefully in the breeze')",
    "camera_style": "Suggested camera movement (e.g., 'slowly pans around', 'smoothly circles')",
    "key_feature": "The standout feature to highlight (e.g., 'vibrant floral pattern')",
    "mood": "Overall mood/atmosphere (e.g., 'dreamy, romantic, aspirational')",
    "colors": ["list", "of", "main", "colors"],
    "style_tags": ["list", "of", "style", "descriptors"]
}

Respond ONLY with the JSON object, no additional text."""

        response = client.models.generate_content(
            model=os.getenv("MODEL_NAME_GENERATED_1"),
            contents=[image_part, prompt],
        )

        # Parse the JSON response
        response_text = response.text.strip()
        # Remove markdown code blocks if present
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        metadata = json.loads(response_text)

        return {
            "status": "success",
            "image_filename": image_filename,
            "metadata": metadata,
        }

    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "message": f"Failed to parse Gemini response as JSON: {e!s}",
            "raw_response": response.text if "response" in dir() else None,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to analyze image: {e!s}",
        }


def add_seed_image(campaign_id: int, image_filename: str) -> dict:
    """Add a seed image from the selected/ folder to a campaign.

    Analyzes the image with Gemini and stores the metadata for video generation.

    Args:
        campaign_id: The ID of the campaign to add the image to
        image_filename: Filename of image in the selected/ folder

    Returns:
        Dictionary with image details and analysis metadata
    """
    # Use storage module for both local and GCS mode
    if not storage.image_exists(image_filename):
        # List available images using storage abstraction
        available = storage.list_seed_images()
        return {
            "status": "error",
            "message": f"Image not found: {image_filename}",
            "available_images": available,
        }

    image_path = storage.get_image_path(image_filename)

    with get_db_cursor() as cursor:
        # Check if campaign exists
        cursor.execute(
            "SELECT id, name FROM campaigns WHERE id = ?", (campaign_id,)
        )
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Check if image is already added to this campaign
        cursor.execute(
            """
            SELECT id FROM campaign_images
            WHERE campaign_id = ? AND image_path = ?
        """,
            (campaign_id, image_filename),
        )
        if cursor.fetchone():
            return {
                "status": "error",
                "message": f"Image '{image_filename}' is already added to this campaign",
            }

        # Analyze the image
        analysis_result = analyze_image(image_filename)
        if analysis_result["status"] == "error":
            return analysis_result

        metadata = analysis_result["metadata"]

        # Insert into database
        cursor.execute(
            """
            INSERT INTO campaign_images (campaign_id, image_path, image_type, metadata)
            VALUES (?, ?, 'seed', ?)
        """,
            (campaign_id, image_filename, json.dumps(metadata)),
        )

        image_id = cursor.lastrowid

        return {
            "status": "success",
            "message": f"Image added to campaign '{campaign['name']}'",
            "image": {
                "id": image_id,
                "campaign_id": campaign_id,
                "image_path": image_filename,
                "full_path": image_path,
                "metadata": metadata,
            },
        }


def list_campaign_images(campaign_id: int) -> dict:
    """List all images for a campaign with their metadata.

    Args:
        campaign_id: The ID of the campaign

    Returns:
        Dictionary with list of images and their analysis metadata
    """
    with get_db_cursor() as cursor:
        # Check if campaign exists
        cursor.execute(
            "SELECT id, name FROM campaigns WHERE id = ?", (campaign_id,)
        )
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        # Get images
        cursor.execute(
            """
            SELECT id, image_path, image_type, metadata, created_at
            FROM campaign_images
            WHERE campaign_id = ?
            ORDER BY created_at
        """,
            (campaign_id,),
        )

        images = []
        for row in cursor.fetchall():
            # Use storage module for path resolution and existence check
            image_path = storage.get_image_path(row["image_path"])
            images.append(
                {
                    "id": row["id"],
                    "image_path": row["image_path"],
                    "full_path": image_path,
                    "exists": storage.image_exists(row["image_path"]),
                    "image_type": row["image_type"],
                    "metadata": json.loads(row["metadata"])
                    if row["metadata"]
                    else None,
                    "created_at": row["created_at"],
                }
            )

        return {
            "status": "success",
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "image_count": len(images),
            "images": images,
        }


def list_available_images() -> dict:
    """List all available seed images in the selected/ folder or GCS bucket.

    Returns:
        Dictionary with list of available image filenames
    """
    # Use storage module for both local and GCS mode
    image_filenames = storage.list_seed_images()

    if not image_filenames and storage.get_storage_mode() == "local":
        if not os.path.exists(SELECTED_DIR):
            return {
                "status": "error",
                "message": f"Selected images folder not found: {SELECTED_DIR}",
            }

    images = []
    for filename in image_filenames:
        filepath = storage.get_image_path(filename)
        # For local mode, include file size; for GCS mode, skip size (would require extra API call)
        image_info = {
            "filename": filename,
            "full_path": filepath,
        }
        if storage.get_storage_mode() == "local":
            try:
                image_info["size_bytes"] = os.path.getsize(filepath)
            except OSError:
                image_info["size_bytes"] = None
        images.append(image_info)

    storage_location = (
        SELECTED_DIR
        if storage.get_storage_mode() == "local"
        else f"gs://{GCS_BUCKET or ''}/seed-images/"
    )
    return {
        "status": "success",
        "folder": storage_location,
        "storage_mode": storage.get_storage_mode(),
        "image_count": len(images),
        "images": images,
    }
