# Glasses / Model-on-Fit

> **MCP Tools**: `glasses_vto`, `glasses_enhance`, `glasses_edit_frame` (images) / `glasses_video_generate`, `glasses_video_regenerate` (video) | **ADK Agent**: Routes via Router Agent

Generate videos of models wearing glasses using AI-powered image composition and video generation. This module creates professional marketing videos by combining model photos with product images and animating the result.

## Overview

The glasses module provides two main capabilities:

1. **Model-on-Fit Videos**: Create videos of models wearing glasses using collage-based video generation
2. **Animation Mode**: Animate single images with custom prompts

The pipeline uses:
- **Nano Banana**: Image enhancement, background removal, frame creation
- **Gemini**: Custom prompt generation
- **Veo**: Video generation from collages

## Directory Structure

The glasses functionality is split across image and video VTO capabilities:

```
genmedia4commerce/workflows/image_vto/glasses/
├── main.py                # FastAPI endpoints (create-frame, edit-frame, enhance-image)
├── image_generation.py    # Nano-based image generation (frame creation, enhancement)
└── images/                # Sample glasses images
    └── models/            # Sample model images

genmedia4commerce/workflows/video_vto/glasses/
├── main.py                # FastAPI endpoints (generate-video, templates, prompts)
├── generate_video_util.py # Veo video generation and post-processing
├── custom_template.py     # AI-powered prompt customization
├── glasses_eval.py        # Video glitch detection
├── men_templates.jsonl    # Pre-defined male model templates
├── women_templates.jsonl  # Pre-defined female model templates
└── videos/                # Template videos
    ├── men/
    └── women/

frontend_dev/image_vto/glasses/
├── Glasses.tsx            # Main component (handles both image and video)
├── Glasses.css
└── icons/
```

## Pipeline Overview

### Standard Model-on-Fit Pipeline

```
Model Image + Glasses Image → Crop/Extract → Create Collage → Generate Video → Post-Process
```

1. **Background Removal**: Remove backgrounds from model and glasses images
2. **Collage Creation**: Compose model (front + optional side view) with glasses on a colored background
3. **Video Generation**: Use Veo to animate the collage
4. **Post-Processing**:
   - Trim video to remove green screen frames
   - Detect and filter glitched videos using Gemini
   - Validate face consistency

### Animation Mode Pipeline

```
Single Image → Apply Prompt → Generate Animation
```

A simpler mode that animates a single image without collage creation.

## API Endpoints

### `GET /get_gallery_images`

Get available sample glasses images.

**Output**: JSON
```json
{
  "images": [
    {"url": "/glasses/images/glasses1.png", "name": "glasses1.png"}
  ]
}
```

### `GET /get_templates`

Get available model video templates.

**Output**: JSON
```json
{
  "men": [{"path": "/glasses/videos/men/template1.mp4", "prompt": ""}],
  "women": [{"path": "/glasses/videos/women/template1.mp4", "prompt": ""}]
}
```

### `POST /generate-custom-prompt`

Generate a structured prompt from natural language and images.

**Input**: `multipart/form-data`
- `text`: Natural language description
- `model_image`: Optional model image file
- `product_image`: Optional product image file
- `custom_field_dict`: Optional JSON string with custom fields

**Output**: JSON with structured prompt fields
```json
{
  "transition_sentence": "Instantly transition to:",
  "setting": "modern studio",
  "lighting": "soft natural light",
  "camera_movement": "slow zoom"
}
```

### `POST /generate-animation-prompt`

Generate an enhanced animation prompt from text and image.

**Input**: `multipart/form-data`
- `text`: User's animation description
- `model_image`: Optional model image file

**Output**: JSON
```json
{
  "enhanced_prompt": "A professional model in a sleek modern studio..."
}
```

### `POST /enhance-image`

Generate 4 enhanced variations of an image.

**Input**: `multipart/form-data`
- `image`: Image file to enhance
- `view_type`: View type hint (default: "front")

**Output**: JSON
```json
{
  "enhanced_images": ["base64_img1", "base64_img2", "base64_img3", "base64_img4"],
  "original_filename": "glasses.png"
}
```

### `POST /create-frame`

Create a single frame of a model wearing glasses.

**Input**: `multipart/form-data`
- `prompt`: Scene description
- `product_image`: Primary glasses image (optional)
- `product_image2`: Secondary glasses image (optional)
- `model_image`: Front view model image (optional)
- `model_side_image`: Side view model image (optional)
- `crop_subject`: Whether to crop subject (default: "true")

**Output**: JSON
```json
{
  "frame_image": "base64_encoded_image",
  "original_product_filename": "glasses.png"
}
```

### `POST /edit-frame`

Edit an existing generated frame based on a prompt.

**Input**: `multipart/form-data`
- `prompt`: Edit instructions
- `generated_image`: Image to edit

**Output**: JSON
```json
{
  "edited_frame_image": "base64_encoded_image"
}
```

### `POST /generate-video`

Generate videos from model and product images.

**Input**: `multipart/form-data`
- `prompt`: Video generation prompt
- `model_image`: Front view model image (optional)
- `model_side_image`: Side view model image (optional)
- `product_image`: Glasses image (optional)
- `number_of_videos`: Number of variations to generate (default: 4)
- `is_template_product_image`: Whether product image is from template (default: "false")
- `background_color`: RGBA color string (default: "0,215,6,255" - green)
- `zoom_level`: Zoom level 0-6 (default: 0)
- `is_animation_mode`: Use animation mode instead of collage (default: "false")

**Output**: JSON
```json
{
  "videos": ["base64_video1", "base64_video2", ...],
  "filenames": ["collage_video_uuid.mp4", ...],
  "collage_data": "base64_encoded_collage"
}
```

### `POST /regenerate-video`

Regenerate videos using existing collage data.

**Input**: JSON
```json
{
  "prompt": "Video prompt...",
  "collage_data": "base64_encoded_collage",
  "number_of_videos": 1,
  "bg_color": "0,215,6,255",
  "is_animation_mode": false
}
```

**Output**: JSON (same as `/generate-video`)

### `POST /merge-videos`

Merge multiple video clips with speed adjustments.

**Input**: `multipart/form-data`
- `videos`: List of video files
- `speeds`: JSON array of playback speeds

**Output**: `video/mp4`

## Key Components

### `image_generation.py`

Handles Nano-based image operations.

```python
def enhance_photo_nano(client, image_bytes, view_type="front") -> bytes:
    """Generate enhanced variation of product image"""

def create_frame_nano(client, prompt, product_image_bytes, ...) -> bytes:
    """Create a single frame of model wearing glasses"""

def edit_frame_nano(client, prompt, image_bytes) -> bytes:
    """Edit an existing generated frame"""
```

### `generate_video_util.py`

Video generation and processing.

```python
def create_collage(model_image_bytes, glasses_image_bytes, ...) -> bytes:
    """Create input collage for Veo from model and product images"""

def generate_veo(client, collage_img, veo_prompt, total_videos, duration_seconds) -> list[bytes]:
    """Generate multiple video variations using Veo"""

def process_veo_video_model_on_fit(video_bytes, bgcolor) -> bytes:
    """Post-process video: trim green screen, validate faces"""
```

### `custom_template.py`

AI-powered prompt generation.

```python
def generate_custom_template(genai_client, text_prompt, ...) -> str:
    """Generate structured prompt JSON from natural language"""

def generate_animation_prompt(genai_client, text_prompt, model_image_bytes) -> str:
    """Generate enhanced animation prompt"""
```

### `glasses_eval.py`

Video quality evaluation.

```python
def check_video_for_glitches(client, video_bytes) -> dict:
    """Check video for visual glitches using Gemini
    Returns: {"is_glitched": bool, "reason": str}"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `GENAI_LOCATION` | Gemini API location (default: "global") |
| `VEO_LOCATION` | Veo API location (default: "global") |
| `NANO_LOCATION` | Nano Banana API location (default: "global") |

## Collage Structure

The collage combines images on a solid background (default: green for chroma-keying):

```
┌─────────────────────────────────────┐
│                                     │
│   [Model Front]    [Model Side]     │
│                                     │
│          [Glasses Image]            │
│                                     │
└─────────────────────────────────────┘
```

The `zoom_level` parameter (0-6) controls the margin around images.

## Post-Processing

Videos go through several post-processing steps:

1. **Green Screen Removal**: Detect and trim frames with solid green background
2. **Face Validation**: Ensure faces are present and consistent
3. **Glitch Detection**: Use Gemini to identify visual artifacts or glitches
4. **Failed Video Filtering**: Remove videos that fail any validation

## Usage Example

```python
import requests
import base64

# Create a model-on-fit video
with open("model_front.jpg", "rb") as f:
    model_data = f.read()
with open("glasses.png", "rb") as f:
    glasses_data = f.read()

response = requests.post(
    "http://localhost:8000/api/glasses/generate-video",
    data={
        "prompt": "A professional model wearing stylish sunglasses in a modern studio",
        "number_of_videos": 2,
        "background_color": "0,215,6,255",
        "zoom_level": 3,
    },
    files={
        "model_image": ("model.jpg", model_data),
        "product_image": ("glasses.png", glasses_data),
    },
)

result = response.json()
for i, video_b64 in enumerate(result["videos"]):
    with open(f"output_{i}.mp4", "wb") as f:
        f.write(base64.b64decode(video_b64))
```

## Templates

Pre-defined templates are stored in JSONL files:
- `men_templates.jsonl`: Male model video templates
- `women_templates.jsonl`: Female model video templates

Each template contains a video path and associated prompt metadata.

## Troubleshooting

### "All videos failed processing"
- The model's face may not be clearly visible
- Try using a front-facing model image
- Reduce `number_of_videos` to see if any pass validation

### Glitched videos being rejected
- This is expected behavior - the glitch detection filters low-quality outputs
- Generate more variations to get valid results

### Green screen not removed properly
- Ensure `background_color` matches the actual collage background
- The default green (0,215,6,255) works best for post-processing
