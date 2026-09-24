# Other Products Spinning (R2V)

> **MCP Tool**: `run_spinning_other_r2v` | **ADK Agent**: Routes via Router Agent

Generate 360-degree spinning videos of any product using AI-powered reference-to-video (R2V) technology. This module handles generic products (not shoes) -- bags, electronics, accessories, furniture, and more. Takes product images from multiple angles and creates a smooth, professional spinning video.

## Overview

The generic product spinning pipeline uses Google Vertex AI to:

1. **Select best images**: If more than 4 images provided, classify product type and view angles, then select the best 4 based on coverage and quality
2. **Preprocess**: Remove backgrounds, upscale images, and arrange onto reference canvases
3. **Generate prompt**: Use Gemini to describe the product and render a Veo prompt from a template
4. **Generate video**: Create an 8-second spinning video using Veo 3.1 R2V mode
5. **Validate**: Check rotation direction (reverse if anticlockwise) and detect glitches
6. **Retry**: Regenerate up to 5 times if validation fails

The pipeline uses:
- **Gemini**: Product description, image classification, and glitch detection
- **Veo 3.1 R2V**: Reference-to-video generation with asset reference images
- **Imagen**: Image upscaling (4x)

## Directory Structure

```
genmedia4commerce/workflows/spinning/r2v/other/
├── pipeline.py              # Veo R2V video generation (single video call)
├── image_selection.py       # Product type classification and best-image selection
├── r2v_utils.py             # Prompt template and product description generation
└── images/                  # Sample product images

genmedia4commerce/mcp_server/spinning/r2v/other/
├── other_mcp.py             # MCP tool: run_spinning_other_r2v
└── other_api.py             # REST API router
```

## Pipeline Overview

```
Product Images -> Select Best 4 -> Preprocess -> Stack & Canvas -> Describe Product -> Generate Video -> Validate Direction -> Glitch Check -> Retry if Needed
```

### Stage 1: Image Selection

When more than 4 images are provided, Gemini classifies each image:

- **Product type**: `shoes`, `cars`, `other` (determines layout priority)
- **View angle**: `right`, `left`, `front`, `back`, `other`
- **Quality**: 1-10 rating

Selection strategy depends on product type:
- **3D objects** (shoes, cars): Side views get solo canvases; front/back are stacked
- **Flat objects** (other): Front/back get solo canvases; sides are stacked

### Stage 2: Preprocessing

1. **Background removal**: Extract product from background using Gemini
2. **Upscaling**: 4x upscale using Imagen for higher quality reference frames
3. **Stacking and canvas**: Arrange images onto reference canvases matching the layout strategy

### Stage 3: Prompt Generation

1. Gemini analyzes all product images and generates a short description (type + primary color)
2. Description is rendered into a Veo prompt template that specifies a continuous 360-degree camera orbit around the stationary product on a white background

### Stage 4: Video Generation with Validation

1. **Generate**: Create 8-second video using Veo 3.1 R2V with reference images as assets
2. **Direction check**: Verify rotation direction using frame analysis
   - Clockwise: keep as-is
   - Anticlockwise: reverse the video
   - Invalid: retry generation
3. **Glitch detection**: Use Gemini to check for visual artifacts
4. **Retry**: If direction is invalid or glitches detected, regenerate (up to 5 retries)

## Key Components

### `pipeline.py`

```python
def generate_video_r2v(reference_images_bytes, prompt, index) -> bytes:
    """Generate a single R2V spinning video using Veo 3.1.
    Uses reference images as 'asset' type references."""
```

### `image_selection.py`

```python
def classify_product_images(client, images_bytes, model) -> dict:
    """Classify product type and view angle for each image.
    Returns: {product_type, classifications: [{index, view, quality}]}"""


def select_best_images(client, images_bytes, model) -> list[bytes]:
    """Select the best 4 images ordered for stack_and_canvas_images.
    Layout depends on product type (3D vs flat objects)."""
```

### `r2v_utils.py`

```python
VEO_R2V_PROMPT_TEMPLATE = """
[Subject]: {{description}}
[Action]: One continuous, seamless, very fast 360-degree orbit around the stationary product...
[Scene]: A completely white studio void...
"""


def generate_product_description(client, gemini_model, all_images_bytes) -> str:
    """Generate a short product description (type + color).
    Returns: 'A [type] standing still in a completely white studio void...'"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `LOCATION` | GCP region for Gemini API calls (default: "europe-west4") |
| `NANO_LOCATION` | Veo/Imagen API location (default: "global") |

### MCP Tool Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `images_base64` | Yes | -- | List of base64-encoded product images (1-4 recommended) |

### Pipeline Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_CONSISTENCY_RETRIES` | 5 | Maximum retry attempts for validation failures |

## MCP Tool Response

```json
{
  "video_base64": "base64-encoded MP4 video",
  "description": "A red ceramic mug standing still...",
  "prompt": "Full Veo prompt used for generation",
  "retries": 1,
  "is_valid": true
}
```

## Troubleshooting

### Invalid rotation direction
- The pipeline automatically reverses anticlockwise videos
- If rotation is classified as "invalid" (no consistent spin), it retries up to 5 times
- Provide images from clearly different angles for better results

### Glitch detection failures
- Gemini checks for visual artifacts after generation
- Failed videos are regenerated automatically
- If all retries fail, the last generated video is returned with `is_valid: false`

### Poor video quality
- Provide high-resolution product images from multiple distinct angles
- Include front, back, left, and right views for best coverage
- Images are upscaled automatically, but starting quality matters
