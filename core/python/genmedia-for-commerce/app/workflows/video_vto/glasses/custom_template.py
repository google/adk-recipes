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

"""Custom template generation for glasses commercials."""

from fastapi import HTTPException
from pydantic import BaseModel, Field
from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config


class StructuredPrompt(BaseModel):
    """Structured prompt schema for glasses commercials."""

    subject: str = Field(
        description="The description of the model appearance. Be specific on gender, optional on ethnicity and include details about the face traits, the hair and the model clothes. Use the following template: A beautiful [gender] model is wearing the same [accurate glasses description up to 10 words] [eyeglasses|sunglasses] as in the picture. [She|He] [Appearance and clothing description]. If the user specify any characteristics of the model, for instance appearance or clothes, these have the preference with respect to what you see in the image; in this case enhance the user input. Return up to three sentences."
    )
    action: str = Field(
        description="The action description of what the model is doing. Use your expertise in commercials to comeup with amazing expressions and actions or poses."
    )
    scene: str = Field(
        description="The scene where we are shooting the commercial. If nothing is specified return a simple: 'Clean, minimalist studio environment with a uniform, soft grey background.'"
    )
    camera_angles_and_movements: str = Field(
        description="Camera angles and movements specifics. If nothing is specified return a simple: 'A static close-up shot, framed from the chest up."
    )
    eyeglasses: str | None = Field(
        description="A one sentence description of the main features of these eyeglasses, including material, frame color and shapes, and temple's color and shapes. Return None if the images are about sunglasses instead of eyeglasses."
    )
    sunglasses: str | None = Field(
        description="A one sentence description of the main features of these sunglasses, including material, frame color and shapes, and temple's color and shapes. Return None if the images are about eyeglasses instead of sunglasses."
    )
    lighting: str = Field(
        description="The lighting settings. If nothing is specified in the text prompt, always return: 'Uniform lighting and no reflection on the (eyeglasses|sunglasses) lenses.'. Use eyeglasses or sunglasses based on the classification of the images."
    )
    custom_field: str | None = Field(
        description="A custom field that we want to generate using the existing contextual information of the field itself and the prompt draft text. If the field contains instructions from the user on how to generate the field itself, follow them; if it contains a draft from the user improve it. Return None if no custom field is provided in input."
    )


def generate_custom_template(
    genai_client,
    text_prompt: str,
    custom_field_dict: dict,
    model_image_bytes: bytes,
    product_image_bytes: bytes,
) -> dict:
    """Generate a structured prompt for glasses commercials using Gemini."""
    system_prompt = """You are an expert of glasses and video director. You have run glasses commercials for your whole life.
Your role is to create clear and well written structured prompt to define the next glasses commercial.
The user is going to send a text draft that you need to improve and extend.
The user might send text in Italian, English or both but your answer **must always be in English**
Your output should be a valid JSON that follows the schema provided.
"""

    user_prompt = """Create the structured prompt given my text draft and the images in input.
If some information are not provided within the draft, return some fields that make sense based on the context provided. If some information are very general like: 'an asian woman', extend this description according to the JSON schema field definition.
The following images are useful to further customize the prompt and to understand if we have sunglasses or eyeglasses in input. Here are the images:\n"""

    custom_field_dict_text = ""
    if custom_field_dict:
        k = next(iter(custom_field_dict))
        v = custom_field_dict[k]
        custom_field_dict_text = f"Here is the custom field key I want your help to refine: '{k}'. This is its current value: '{v}' Rewrite and improve the value and place it in the field 'custom_field' of your output. If the user input is incomplete, complete it to the best of your knowledge. If it contains instructions on how to fill the field, consider them."

    # Build content pieces (text and images)
    images = [
        img
        for img in [model_image_bytes, product_image_bytes]
        if img is not None
    ]
    content_pieces = [
        user_prompt,
        *images,
        f"Here is my draft:\n{text_prompt}",
        custom_field_dict_text,
        "Please return the structured prompt.",
    ]

    # Build config with system instruction
    config = get_generate_content_config(
        temperature=0,
        response_mime_type="application/json",
        response_schema=StructuredPrompt,
    )
    config.system_instruction = system_prompt

    try:
        return generate_gemini(
            text_images_pieces=content_pieces,
            client=genai_client,
            config=config,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate prompt: {e}"
        ) from e


def generate_animation_prompt(
    genai_client, text_prompt: str, model_image_bytes: bytes | None = None
) -> str:
    """Enhance an animation prompt using AI expertise in video and glasses commercials.

    Return a plain text enhanced prompt.
    """
    system_prompt = """You are an expert copy writer and editor.
Your role is to enhance and improve the user's prompt about generating an animation from a frame.
The user might send text in Italian, English or both but your answer **must always be in English**.
Your task is to rephrase and write better what the user inserts while maintaining the user's original intent.
The prompt should have the tone of describing the scene rather than giving instruction. For instance instead of using: 'Maintain naturalistic facial expressions, ensuring the eyewear remains the hero product.', use 'The facial expressions are natural and the eyewear remains the hero product.'
Return only the enhanced prompt text, nothing else. The text should not be longer than 5 sentences and should use simple language. The major focus should be on the glasses and the actions that the user want the model to perform. Do not change too much the original user's intent."""

    user_prompt = f"""Please enhance and improve the following animation prompt.
Focus on smooth, natural movements with professional timing and realistic facial expressions.
Ensure the enhanced prompt maintains focus on the eyewear throughout the sequence and use simple language.

Original prompt: {text_prompt}"""

    # Build content pieces
    content_pieces = [user_prompt]
    if model_image_bytes:
        content_pieces.append(model_image_bytes)
        content_pieces.append(
            "Use this model image for additional context about the glasses and model appearance."
        )

    # Build config with system instruction
    config = get_generate_content_config(temperature=0)
    config.system_instruction = system_prompt

    try:
        return generate_gemini(
            text_images_pieces=content_pieces,
            client=genai_client,
            config=config,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate animation prompt: {e}"
        ) from e
