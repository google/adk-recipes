# Clothes / Virtual Try-On (VTO)

> **MCP Tool**: `image_vto_clothes` | **REST API**: `/api/clothes/generate-vto` | **ADK Agent**: Routes via Router Agent

Virtual try-on is a **B2C consumer-facing** tool: it takes a real person's photo and one or more garment images, then generates realistic images of that person wearing those garments while preserving their face and body characteristics. Unlike product fitting (B2B catalogue enrichment with a single product on a preset AI model), VTO supports multiple garments and uses the customer's own photo.

## Overview

The Virtual Try-On module:
1. Preprocesses face and body images separately for optimal results
2. Generates try-on images using Nano Banana
3. Evaluates output quality by comparing face similarity
4. Streams results as they complete for responsive UX

## Directory Structure

```
genmedia4commerce/
├── workflows/image_vto/clothes/
│   ├── vto_generation.py        # Core VTO generation and evaluation logic
│   ├── garment_description.py   # Garment description via Gemini
│   └── garment_eval.py          # VTO image evaluation
├── mcp_server/image_vto/
│   ├── clothes_mcp.py           # MCP tool: image_vto_clothes
│   └── clothes_api.py           # REST API router
└── frontend_dev/image_vto/clothes/
    ├── ClothesImageVTO.tsx      # Image VTO component
    └── ClothesImageVTO.css
```

## Pipeline Overview

```
Face Image + Body Image + Garments → Preprocess → Generate VTO → Evaluate Face Similarity
```

### Preprocessing Stage

1. **Face Processing**:
   - Detect and crop face region
   - Upscale cropped face for reference (used in evaluation)
   - Remove background from face image

2. **Body Processing**:
   - Remove background from body image
   - Prepare for compositing with garments

### Generation Stage

1. Combine preprocessed face, body, and garment images
2. Generate try-on image using Nano Banana
3. Apply scene/environment settings

### Evaluation Stage

1. Detect face in generated image
2. Compare with reference face using embedding similarity
3. Return similarity percentage as quality metric

## API Endpoints

### `POST /generate-vto`

Generate multiple VTO variations with SSE streaming. Preprocessing is done automatically - no separate preprocessing step required.

**Input**: `multipart/form-data`
- `face_image`: Face/portrait image file
- `full_body_image`: Full body image file
- `garments`: List of garment image files
- `scenario`: Scene description (default: "a plain light grey studio environment")
- `num_variations`: Number of variations to generate (default: 4)

**Output**: SSE stream

Each variation streams as soon as generation AND evaluation complete:
```
data: {"index": 0, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 94.3, "face_detected": true}}

data: {"index": 2, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 92.1, "face_detected": true}}

data: {"index": 1, "status": "failed", "error": "Generation failed"}

data: {"index": 3, "status": "ready", "image_base64": "...", "evaluation": {"similarity_percentage": 88.3, "face_detected": true}}

data: {"status": "complete", "total": 4}
```

**Errors**:
- `400`: "No face detected in the face photo. Please upload a clearer image with a visible face."

**Processing Flow**:
1. Face and body preprocessing run in parallel
2. All variations start generating in parallel
3. Each variation is evaluated immediately after generation
4. Results stream as each variation completes (not in order)

## Key Components

### `vto_generation.py`

Core VTO functionality.

```python
def preprocess_face_image(client, face_bytes) -> tuple[bytes, bytes]:
    """Preprocess face image
    Returns: (reference_face_cropped_upscaled, preprocessed_face_no_bg)"""


def preprocess_model_image(client, body_bytes) -> bytes:
    """Preprocess body image (background removal)
    Returns: preprocessed_body_bytes"""


def generate_vto(
    client, scenario, garment_images, preprocessed_person_images
) -> bytes:
    """Generate VTO image
    Returns: result_image_bytes or None on failure"""


def evaluate_vto_image(vto_image_bytes, reference_face_bytes) -> dict:
    """Evaluate face similarity
    Returns: {"similarity_percentage": float}"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `NANO_LOCATION` | Nano Banana API location (default: "global") |

## Workflow Pattern

Use the single `/generate-vto` endpoint with SSE streaming:

```python
import requests
import json

# Generate multiple variations with SSE streaming
response = requests.post(
    "/api/clothes/generate-vto",
    files={
        "face_image": open("face.jpg", "rb"),
        "full_body_image": open("body.jpg", "rb"),
        "garments": open("shirt.jpg", "rb"),
    },
    data={"num_variations": 4, "scenario": "fashion studio"},
    stream=True,
)

# Process SSE events as they arrive
for line in response.iter_lines():
    if line and line.startswith(b"data: "):
        data = json.loads(line[6:])
        if data.get("status") == "ready":
            print(
                f"Variation {data['index']}: {data['evaluation']['similarity_percentage']:.1f}%"
            )
            # Save or display the image
            image_bytes = base64.b64decode(data["image_base64"])
        elif data.get("status") == "failed":
            print(f"Variation {data['index']} failed: {data.get('error')}")
        elif data.get("status") == "complete":
            print(f"All {data['total']} variations complete")
```

## Evaluation Metrics

### Face Similarity Percentage

The evaluation compares the face in the generated image to the reference face:

| Score | Quality |
|-------|---------|
| 90%+ | Excellent - face is highly preserved |
| 80-90% | Good - minor differences |
| 70-80% | Acceptable - noticeable changes |
| <70% | Poor - significant face distortion |

Use similarity scores to filter and rank results.

## Usage Example

```python
import requests
import base64
import json

# Prepare images
files = {
    "face_image": ("face.jpg", open("face.jpg", "rb"), "image/jpeg"),
    "full_body_image": ("body.jpg", open("body.jpg", "rb"), "image/jpeg"),
    "garments": ("shirt.jpg", open("shirt.jpg", "rb"), "image/jpeg"),
}

# Generate VTO with SSE streaming
response = requests.post(
    "http://localhost:8000/api/clothes/generate-vto",
    files=files,
    data={
        "scenario": "a modern fashion studio with soft lighting",
        "num_variations": 4,
    },
    stream=True,
)

# Collect results as they stream
results = []
for line in response.iter_lines():
    if line and line.startswith(b"data: "):
        data = json.loads(line[6:])
        if data.get("status") == "ready":
            results.append(data)
            print(
                f"Variation {data['index']}: {data['evaluation']['similarity_percentage']:.1f}%"
            )

# Save best result (highest similarity)
best = max(results, key=lambda x: x["evaluation"]["similarity_percentage"])
with open("vto_result.png", "wb") as f:
    f.write(base64.b64decode(best["image_base64"]))
print(
    f"Saved best result with {best['evaluation']['similarity_percentage']:.1f}% similarity"
)
```

## Multiple Garments

You can combine multiple garment images:

```python
files = [
    ("garments", ("top.jpg", open("top.jpg", "rb"))),
    ("garments", ("pants.jpg", open("pants.jpg", "rb"))),
    ("garments", ("shoes.jpg", open("shoes.jpg", "rb"))),
]
```

The AI will attempt to dress the person in all provided garments.

## Troubleshooting

### "No face detected in the face photo"
- Ensure the face is clearly visible and well-lit
- Use a front-facing portrait
- Avoid images where face is partially obscured

### Low similarity scores
- Try different face/body image combinations
- Use higher quality input images
- Ensure consistent lighting between face and body images

### Generation failures
- Some garment/person combinations may be challenging
- Generate multiple variations to increase success rate
- Simplify the scenario description

### Slow streaming
- SSE results are returned as they complete (not in order)
- Some variations may take longer than others
- All variations run in parallel for optimal throughput
