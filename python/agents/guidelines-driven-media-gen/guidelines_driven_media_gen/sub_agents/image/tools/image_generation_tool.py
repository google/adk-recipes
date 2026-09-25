from datetime import datetime
import os
import sys
import traceback
from google import genai
from google.genai import types
from google.adk.tools import ToolContext
from google.cloud import storage
from .... import config
import logging

logger = logging.getLogger(__name__)

client = genai.Client(
    vertexai=True, 
    project=os.environ.get("GOOGLE_CLOUD_PROJECT"), 
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            initial_delay=1.0,
            attempts=5,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
        timeout=120 * 1000,
    ), 
)

async def generate_images(image_gen_prompt: str, reference_images: list[str], tool_context: ToolContext):
    logger.info("Entered generate_images tool.")
    logger.debug(f"image_gen_prompt: {image_gen_prompt}")
    logger.debug(f"reference_images: {reference_images}")
    try:
        storage_client = storage.Client()
        if not reference_images:
            raise ValueError("No reference images provided")
            
        image_path_or_uri = reference_images[0]
        if image_path_or_uri.startswith("gs://"):
            # Remove gs:// prefix and split into bucket and blob path
            path_parts = image_path_or_uri[5:].split("/", 1)
            if len(path_parts) != 2:
                raise ValueError(f"Invalid GCS URI format: {image_path_or_uri}")
                
            bucket_name, blob_name = path_parts
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            image_bytes = blob.download_as_bytes()
        else:
            # Assume local relative path and resolve via data directory
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            abs_path = os.path.join(base_dir, "data", image_path_or_uri)
            if not os.path.exists(abs_path):
                if os.path.exists(image_path_or_uri):
                    abs_path = image_path_or_uri
                else:
                    raise FileNotFoundError(f"Local image file not found: {image_path_or_uri} (also tried {abs_path})")
            with open(abs_path, "rb") as f:
                image_bytes = f.read()

        msg1_image1 = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/png",
        )
        msg1_text1 = types.Part.from_text(text=image_gen_prompt)
        
        model = config.IMAGE_GEN_MODEL
        contents = [
            types.Content(
                role="user",
                parts=[
                    msg1_image1,
                    msg1_text1
                ]
            ),
        ]
        
        
        generate_content_config = types.GenerateContentConfig(
            temperature = 1,
            top_p = 0.95,
            max_output_tokens = 32768,
            response_modalities = ["TEXT", "IMAGE"],
            safety_settings = [types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH",
            threshold="OFF"
            ),types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT",
            threshold="OFF"
            ),types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
            threshold="OFF"
            ),types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT",
            threshold="OFF"
            )],
            system_instruction=[types.Part.from_text(text=config.IMAGE_GEN_SYSTEM_INSTRUCTION)],
            image_config=types.ImageConfig(
                aspect_ratio="1:1",
                image_size="1K",
                output_mime_type="image/png",
            ),
        )
        response = client.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
        )
        generated_image_part = None
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    generated_image_part = part
                    break
        
        if generated_image_part:
            image_bytes = generated_image_part.inline_data.data
            counter = str(tool_context.state.get("loop_iteration", 0))
            artifact_name = f"generated_image_{counter}.png"

            if config.GCS_BUCKET_NAME:
                save_result = save_to_gcs(tool_context, image_bytes, artifact_name, counter)
                if save_result and save_result.get("status") == "error":
                    # Propagate the GCS upload error instead of saying success
                    return save_result

            # --- Save to Local Folder ---
            # Below block is helpful for local testing using `adk web`. Remove or comment this block after after local testing 
            """
            try:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
                local_dir = os.path.join(base_dir, "data", "generated_images")
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, artifact_name)
                with open(local_path, "wb") as f:
                    f.write(image_bytes)
                logger.info(f"Saved generated image to {local_path}")
            except Exception as e_local:
                logger.error(f"Failed to save image to local folder: {e_local}")
            """

            report_artifact = types.Part.from_bytes(
                data=image_bytes, mime_type="image/png"
            )
            await tool_context.save_artifact(artifact_name, report_artifact)

            return {
                "status": "success",
                "message": f"Image generated. ADK artifact: {artifact_name}.",
                "artifact_name": artifact_name,
            }
        else:
            error_details = str(response)
            logger.error(f"No images generated. Response: {error_details}")
            tool_context.actions.escalate = True
            return {
                "status": "error",
                "message": f"No images generated. Response: {error_details}",
            }

    except Exception as e:
        traceback.print_exc(file=sys.stdout)
        logger.error(f"Error generating images: {e}", exc_info=True)
        tool_context.actions.escalate = True
        return {"status": "error", "message": f"No images generated. {e}"}


def save_to_gcs(tool_context: ToolContext, image_bytes, filename: str, counter: str):
    # --- Save to GCS ---
    storage_client = storage.Client()  # Initialize GCS client
    bucket_name = config.GCS_BUCKET_NAME

    unique_id = tool_context.state.get("unique_id", "")
    current_date_str = datetime.utcnow().strftime("%Y-%m-%d")
    unique_filename = filename
    gcs_blob_name = f"{current_date_str}/{unique_id}/{unique_filename}"

    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(gcs_blob_name)

    try:
        blob.upload_from_string(image_bytes, content_type="image/png")
        gcs_uri = f"gs://{bucket_name}/{gcs_blob_name}"
        logger.info(f"Uploaded image to GCS bucket: {bucket_name} at {gcs_uri}")

        # Store GCS URI in session context
        tool_context.state["generated_image_gcs_uri_" + counter] = gcs_uri

    except Exception as e_gcs:

        # Decide if this is a fatal error for the tool
        return {
            "status": "error",
            "message": f"Image generated but failed to upload to GCS: {e_gcs}",
        }
        # --- End Save to GCS ---
