# Virtual Try-On Architecture

## Overview

Try-on supports both Image VTO (via general-purpose Gemini image models: gemini-2.5-flash-image (default) or gemini-2.5-pro-image) and Video VTO (via Veo 3.1 catwalk video animations).

```
User Photo + Product Image
    |
    |-- VTO Image Generation (generate_tryon_image)
    |     |-- Route to gemini-2.5-flash-image (default) / gemini-2.5-pro-image
    |     v
    |   [Composite Try-On Image]
    |
    +-- IF Video Mode Enabled (generate_tryon_video):
          |-- Split composite into framings: Lower Body, Upper Body, Face
          |-- Pad each framing onto a 16:9 canvas
          |-- Invoke Veo 3.1 R2V model (veo-3.1-generate-001)
          v
        [Catwalk Video mp4]
```

## Key Components

### tryon_agent.py
- Exposes ADK tool wrappers: `try_on_image_tool` and `try_on_video_tool`.
- Reads project, region, models, and buckets from environment.

### tryon_processor.py
- `generate_tryon_image()`: Generates try-on using `client.models.generate_content` (Gemini path) with high-fidelity system prompts and user prompts.
- `generate_tryon_video()`: Crops try-on output into three reference frames, pads to 16:9, and calls Veo 3.1 `generate_videos` R2V API.

## API / SDK reference

### Image VTO (Gemini Image Generation)
```python
response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[
        types.Part.from_bytes(data=person_bytes, mime_type="image/jpeg"),
        types.Part.from_bytes(data=product_bytes, mime_type="image/jpeg"),
        user_task
    ],
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        system_instruction=system_prompt,
        temperature=0.1
    )
)
```

### Video VTO (Veo Reference-to-Video R2V)
```python
from google.genai.types import Image, VideoGenerationReferenceImage

ref_images_list = []
for img_bytes in [lower_body_png, upper_body_png, face_png]:
    ref_image = VideoGenerationReferenceImage(
        image=Image(imageBytes=img_bytes, mime_type="image/png"),
        reference_type="asset"
    )
    ref_images_list.append(ref_image)

operation = client.models.generate_videos(
    model="veo-3.1-generate-001",
    prompt="catwalk animation prompt",
    config=types.GenerateVideosConfig(
        aspect_ratio="16:9",
        number_of_videos=1,
        duration_seconds=5,
        reference_images=ref_images_list,
        person_generation="allow_adult"
    )
)
```
