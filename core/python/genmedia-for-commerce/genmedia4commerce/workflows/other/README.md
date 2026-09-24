# Other Products

> **MCP Tools**: `spinning_other_r2v`, `spinning_interpolation`, `background_changer` | **ADK Agent**: Routes via Router Agent

A collection of generic product video and image generation tools that work with any product type. This module provides three main capabilities: frame interpolation, reference-to-video (R2V) spinning, and background changing.

## Overview

| Feature | Description |
|---------|-------------|
| **Interpolation** | Generate smooth transition videos between product frames |
| **R2V (Reference-to-Video)** | Create 360° spinning videos from product images |
| **Background Changer** | Replace backgrounds in person images while preserving face identity |

## Directory Structure

The "other" functionality is split across multiple capability directories:

```
genmedia4commerce/workflows/spinning/r2v/other/
├── main.py                  # FastAPI endpoints for R2V spinning
├── r2v_utils.py             # R2V prompt generation
└── images/products_r2v/     # R2V product samples

genmedia4commerce/workflows/spinning/interpolation/other/
├── main.py                  # FastAPI endpoints for interpolation
├── interpolation_utils.py   # Frame interpolation logic
└── images/products_interpolation/  # Interpolation samples

genmedia4commerce/workflows/shared/
└── image_utils.py           # Shared image preprocessing (preprocess_images)

genmedia4commerce/workflows/other/
├── main.py                  # FastAPI endpoints for background changer
└── background_changer/
    └── background_changer.py  # Background replacement logic

frontend_dev/spinning/r2v/other/
└── SpinningR2V.tsx          # R2V spinning component

frontend_dev/spinning/interpolation/other/
├── SpinningInterpolation.tsx  # Interpolation component
├── Spinning.css
└── InteractiveViewer.tsx

frontend_dev/other/background_changer/
└── BackgroundChanger.tsx    # Background changer component
```

## Feature 1: Frame Interpolation

Generate smooth transition videos between product frames. Useful for creating product showcase videos from a sequence of still images.

### Pipeline

```
Images → Preprocess (BG removal + upscale + canvas) → Generate Transitions → Merge
```

### Endpoints

#### `POST /interpolation-preprocess`

Preprocess images for interpolation.

**Input**: `multipart/form-data`
- `images`: List of image files

**Output**: JSON
```json
{
  "images": [
    {"index": 0, "data": "base64_image"},
    {"index": 1, "data": "base64_image"}
  ]
}
```

#### `POST /interpolation-generate-prompt`

Generate interpolation prompt (currently returns a static optimized prompt).

**Input**: `multipart/form-data`
- `img1`: First frame image
- `img2`: Second frame image

**Output**: JSON
```json
{
  "prompt": "Smoothly transition between the two product views..."
}
```

#### `POST /interpolation-generate`

Generate a single transition video between two frames.

**Input**: `multipart/form-data`
- `img1`: Start frame image
- `img2`: End frame image
- `index`: Segment index
- `prompt`: Transition prompt
- `backgroundColor`: Background color hex (default: "#FFFFFF")

**Output**: `video/mp4`

#### `POST /interpolation-merge`

Merge multiple video segments with speed adjustments.

**Input**: `multipart/form-data`
- `videos`: List of video files
- `speeds`: JSON array of playback speeds

**Output**: `video/mp4`

### Usage Example

```python
import requests
import base64

# Step 1: Preprocess images
preprocess_response = requests.post(
    "/api/spinning/interpolation/other/interpolation-preprocess",
    files=[("images", open(f"frame_{i}.jpg", "rb")) for i in range(4)],
)
processed = preprocess_response.json()["images"]

# Step 2: Generate transitions between consecutive frames
videos = []
for i in range(len(processed) - 1):
    response = requests.post(
        "/api/spinning/interpolation/other/interpolation-generate",
        files={
            "img1": base64.b64decode(processed[i]["data"]),
            "img2": base64.b64decode(processed[i + 1]["data"]),
        },
        data={
            "index": i,
            "prompt": "Smooth transition",
            "backgroundColor": "#FFFFFF",
        },
    )
    videos.append(response.content)

# Step 3: Merge all transitions
merge_response = requests.post(
    "/api/spinning/interpolation/other/interpolation-merge",
    files=[("videos", v) for v in videos],
    data={"speeds": json.dumps([1.0] * len(videos))},
)

with open("final_video.mp4", "wb") as f:
    f.write(merge_response.content)
```

---

## Feature 2: R2V (Reference-to-Video)

Generate 360° spinning videos of any product using reference images. Similar to shoes spinning but without shoe-specific classification.

### Pipeline

```
Images → Preprocess (upscale + extract) → Stack References → Generate Prompt → Generate Video
```

### Endpoints

#### `POST /r2v-preprocess`

Preprocess product images for R2V generation.

**Input**: `multipart/form-data`
- `images`: List of image files (max 4)

**Output**: JSON
```json
{
  "processed_images": [
    {"index": 0, "image_base64": "..."},
    {"index": 1, "image_base64": "..."},
    {"index": 2, "image_base64": "..."}
  ],
  "num_processed": 3
}
```

Note: Images are stacked to create up to 3 reference images (Veo limit).

#### `POST /r2v-generate-prompt`

Generate a spinning video prompt based on product images.

**Input**: `multipart/form-data`
- `images`: List of image files

**Output**: JSON
```json
{
  "prompt": "A sleek wireless speaker rotates slowly 360 degrees...",
  "description": "wireless bluetooth speaker with metallic finish"
}
```

#### `POST /r2v-generate`

Generate a single spinning video.

**Input**: `multipart/form-data`
- `reference_images`: List of reference image files
- `prompt`: Video generation prompt
- `index`: Video index (default: 0)

**Output**: `video/mp4`

#### `POST /r2v-pipeline`

Full end-to-end R2V pipeline.

**Input**: `multipart/form-data`
- `images`: List of product image files (max 4)

**Output**: `video/mp4`

### Usage Example

```python
# Simple: Use the pipeline endpoint
response = requests.post(
    "/api/spinning/r2v/other/r2v-pipeline",
    files=[("images", open(f"product_{i}.jpg", "rb")) for i in range(4)],
)

with open("spinning_video.mp4", "wb") as f:
    f.write(response.content)
```

---

## Feature 3: Background Changer

Replace backgrounds in person images while preserving face identity. Useful for creating marketing images with different settings.

### Pipeline

```
Person Image → Preprocess (face + person in parallel) → Generate Variations → Evaluate → Stream Results
```

### Endpoint

#### `POST /change-background`

Generate multiple background change variations with SSE streaming. Preprocessing is done automatically.

**Input**: `multipart/form-data`
- `person_image`: Person image file
- `background_description`: Text description of desired background (optional)
- `background_image`: Reference background image (optional)
- `num_variations`: Number of variations to generate (default: 4)

Note: Either `background_description` or `background_image` is required.

**Output**: SSE stream

Each variation streams as soon as generation AND evaluation complete:
```
data: {"index": 0, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 93.6, "face_detected": true}}

data: {"index": 2, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 92.9, "face_detected": true}}

data: {"index": 1, "status": "failed", "error": "Generation failed"}

data: {"index": 3, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 92.7, "face_detected": true}}

data: {"status": "complete", "total": 4}
```

**Errors**:
- `400`: "No face detected in the person image. Please upload a clearer image with a visible face."

**Processing Flow**:
1. Face cropping/upscaling and person preprocessing run in parallel
2. All variations start generating in parallel
3. Each variation is evaluated immediately after generation
4. Results stream as each variation completes (not in order)

### Usage Example

```python
import requests
import json
import base64

# With text description - SSE streaming
response = requests.post(
    "/api/other/change-background",
    files={"person_image": open("person.jpg", "rb")},
    data={
        "background_description": "tropical beach at sunset",
        "num_variations": 4,
    },
    stream=True,
)

# Process SSE events as they arrive
results = []
for line in response.iter_lines():
    if line and line.startswith(b"data: "):
        data = json.loads(line[6:])
        if data.get("status") == "ready":
            results.append(data)
            print(
                f"Variation {data['index']}: {data['evaluation']['similarity_percentage']:.1f}%"
            )
        elif data.get("status") == "complete":
            print(f"All {data['total']} variations complete")

# Save best result
best = max(results, key=lambda x: x["evaluation"]["similarity_percentage"])
with open("background_result.png", "wb") as f:
    f.write(base64.b64decode(best["image_base64"]))

# With reference background image
response = requests.post(
    "/api/other/change-background",
    files={
        "person_image": open("person.jpg", "rb"),
        "background_image": open("beach.jpg", "rb"),
    },
    data={"num_variations": 4},
    stream=True,
)
```

---

## Gallery Endpoint

#### `GET /get_gallery_images`

Get sample product images for any feature.

**Query Parameters**:
- `gallery_type`: One of `"default"`, `"interpolation"`, `"r2v"`

**Output**: JSON
```json
{
  "products": [
    {
      "folder_name": "product_001",
      "images": [
        {"url": "/other/images/products/product_001/front.jpg", "name": "front.jpg"}
      ]
    }
  ]
}
```

---

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `LOCATION` | GCP region for Gemini API |
| `NANO_LOCATION` | Nano Banana API location (default: "global") |

---

## Key Components

### `interpolation_utils.py`

```python
def get_interpolation_prompt() -> str:
    """Get optimized prompt for frame interpolation"""

def process_single_video(client, start_image, end_image, prompt, ...) -> bytes:
    """Generate single transition video between two frames"""
```

### `r2v_utils.py`

```python
VEO_R2V_PROMPT_TEMPLATE = "..."  # Jinja template for spinning prompts


def generate_product_description(client, gemini_model, all_images_bytes) -> str:
    """Generate product description from images using Gemini"""
```

### `background_changer.py`

```python
def preprocess_person_image(client, nano_client, person_bytes) -> bytes:
    """Preprocess person image (background removal, enhancement)"""

def generate_background_change_only(client, preprocessed_person_image, ...) -> bytes:
    """Generate new background for person"""

def evaluate_background_change_image(client, result_image, reference_face) -> dict:
    """Evaluate face similarity in result"""
```

### Shared: `image_utils.py`

Image preprocessing is provided by the shared module. See `workflows/shared/README.md` for details.

```python
from workflows.shared.image_utils import preprocess_images

def preprocess_images(images_bytes_list, client, upscale_client, ...) -> list[bytes]:
    """Batch preprocess images (background removal, upscaling, canvas creation)"""
```

---

## Troubleshooting

### Interpolation videos have artifacts
- Ensure input frames are similar enough for smooth transitions
- Use the preprocessing endpoint to standardize image sizes
- Try reducing the difference between consecutive frames

### R2V video doesn't spin properly
- Provide images from multiple angles (front, side, back)
- Ensure product is clearly extracted from background
- Use 3-4 input images for best results

### Background change has low face similarity
- Use clear, front-facing person images
- Avoid complex poses or partially hidden faces
- Generate multiple variations and select the best one

### "No face detected" error
- Ensure person's face is clearly visible
- Use well-lit, high-resolution images
- Face should be at least ~100x100 pixels in the image
