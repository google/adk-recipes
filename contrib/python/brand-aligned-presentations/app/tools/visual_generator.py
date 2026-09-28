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

import asyncio
import tempfile
import uuid

from google.genai import types

from ..shared_libraries.config import (
    GCS_BUCKET_NAME,
    IMAGE_GENERATION_MODEL,
    get_gcs_client,
    get_logger,
    initialize_genai_client,
)


async def generate_visual(prompt: str) -> str:
    """
    Generates a visual from a single string prompt using a multi-modal model.
    Returns the GCS URI of the generated image or a temp local path if GCS is unconfigured.
    """
    log = get_logger("generate_visual_tool")
    if not prompt:
        return "Error: Prompt cannot be empty."

    # Strip any prefixes like "chart:" or "image:" as the model understands context
    prompt = prompt.strip()
    if prompt.lower().startswith("chart:"):
        # Re-frame the chart prompt as a request for a visual representation
        chart_description = prompt[len("chart:") :]
        prompt = f"A professional data visualization representing a {chart_description}"
    elif prompt.lower().startswith("image:"):
        prompt = prompt[len("image:") :]
    prompt = prompt.strip()

    log.info(f"Dispatching to multi-modal model for prompt: '{prompt}'")

    try:
        log.info("Attempting to generate image...")
        # Step 1: Generate the image bytes
        client = initialize_genai_client()
        if client is None:
            log.error("Failed to initialize GenAI client.")
            return "Error: Visual generation failed."

        model_name = IMAGE_GENERATION_MODEL
        log.info(f"Calling {model_name} to generate visual...")
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="4:3"),
            ),
        )

        # Safely extract the image data from the response
        if (
            response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts
            and response.candidates[0].content.parts[0].inline_data
            and response.candidates[0].content.parts[0].inline_data.data
        ):
            image_bytes = (
                response.candidates[0].content.parts[0].inline_data.data
            )
            log.info(f"Successfully generated visual using {model_name}.")
        else:
            raise RuntimeError(
                "Multi-modal model did not return valid image data."
            )

        # If GCS_BUCKET_NAME is set, upload to GCS. Otherwise, save to temp storage
        if GCS_BUCKET_NAME:
            # Step 2: Upload the image bytes to GCS
            log.info(
                f"Uploading generated image to GCS bucket: {GCS_BUCKET_NAME}"
            )
            storage_client = get_gcs_client()
            if storage_client is None:
                raise RuntimeError("GCS client could not be initialized.")
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            # Create a unique name for the image file
            image_filename = f"generated_images/{uuid.uuid4().hex}.png"
            blob = bucket.blob(image_filename)

            # Upload from the in-memory bytes
            await asyncio.to_thread(
                blob.upload_from_string,
                image_bytes,
                content_type="image/png",
                timeout=60,
            )

            # --- Step 3: Return the GCS URI instead of Base64 data ---
            gcs_uri = f"gs://{GCS_BUCKET_NAME}/{image_filename}"
            log.info(
                f"Visual generated and saved successfully. Returning GCS URI: {gcs_uri}"
            )
            return gcs_uri
        else:
            # Fallback for local / InMemory testing using OS temp directory
            with tempfile.NamedTemporaryFile(
                suffix=".png", delete=False
            ) as tmp:
                tmp.write(image_bytes)
                local_filepath = tmp.name

            log.info(
                f"No GCS Bucket configured. Saved visual to temp file: {local_filepath}"
            )
            return local_filepath

    except Exception as e:
        log.error(
            f"Visual generation or upload FAILED for prompt '{prompt}': {e}",
            exc_info=True,
        )
        return f"Error: Visual generation failed. Details: {e}"
