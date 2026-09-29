# Shoes Spinning

> **MCP Tool**: `spinning_shoes_r2v` | **REST API**: `/api/shoes/spinning/*` | **ADK Agent**: Routes via Router Agent

Generate 360° spinning videos of shoes using AI-powered reference-to-video (R2V) technology. This module takes multiple product images from different angles and creates a smooth, professional spinning video.

## Overview

The shoes spinning pipeline uses Google Vertex AI to:
1. Classify shoe images by viewing angle (front, back, left, right, top, bottom, etc.)
2. Split images containing multiple shoes
3. Upscale and extract products from backgrounds
4. Generate spinning videos using Veo's reference-to-video capability
5. Validate video quality and product consistency

## Directory Structure

```
genmedia4commerce/workflows/spinning/r2v/shoes/
├── main.py                          # FastAPI endpoints
├── pipeline.py                      # Main R2V pipeline orchestration
├── classify_shoes.py                # Shoe angle classification
├── split_multiple_shoes.py          # Multi-shoe image splitting
├── shoe_images_selection.py         # Image ordering and selection
├── prompt_generation_r2v.py         # Veo prompt generation
├── video_validation_r2v.py          # Video spin consistency validation
├── product_consistency_validation.py # Product identity validation
├── images_utils.py                  # Image processing utilities
├── evaluation_app/                  # Video evaluation tools
│   └── evaluation_viewer/           # Web-based evaluation viewer
├── images/products/                 # Sample product images
└── notebooks/                       # Training notebooks

frontend_dev/spinning/r2v/shoes/
├── ShoesSpinning.tsx               # Main component
└── InteractiveViewer.tsx           # Video viewer component
```

## Pipeline Stages

### Stage 1: Image Classification and Preprocessing

```
Input Images → Classification → Split Multi-Shoe → Upscale → Extract Product
```

1. **Classification**: Each image is classified by viewing angle using a custom Vertex AI endpoint
   - Classes: `front`, `back`, `left`, `right`, `top`, `bottom`, `front_left`, `front_right`, `back_left`, `back_right`, `multiple`, `invalid`

2. **Multi-Shoe Splitting**: Images classified as `multiple` are split into individual shoe images using AI segmentation

3. **Upscaling**: Images are upscaled 4x using Imagen for higher quality reference frames

4. **Product Extraction**: Background is removed to isolate the shoe

### Stage 2: Image Selection and Ordering

```
Classified Images → Select Best Angles → Order for Rotation → Stack to 3 References
```

1. **Selection**: Choose the best images based on viewing angles and image quality
2. **Ordering**: Arrange images in rotational order (e.g., right → front → left → back)
3. **Stacking**: Combine images into up to 3 reference images for Veo (max allowed by API)

### Stage 3: Video Generation

```
Reference Images → Generate Prompt → Veo R2V → Validate → Retry if Needed
```

1. **Prompt Generation**: Use Gemini to describe the shoe and generate an optimized Veo prompt
2. **Video Generation**: Generate 8-second video using Veo's reference-to-video mode
3. **Spin Validation**: Verify the video shows consistent rotation
4. **Product Consistency**: Ensure the shoe in the video matches the input images
5. **Retry Logic**: If validation fails, regenerate up to `max_retries` times

### Stage 4: Output Processing

```
Video → Sample Frames → Resize → Upload to GCS (optional)
```

1. **Frame Extraction**: Sample 50 frames from the video
2. **Resize**: Resize frames to 1000x1000 for consistency
3. **GCS Upload**: Optionally upload video and frames to Google Cloud Storage

## API Endpoints

### `POST /preprocess-images-r2v`

Preprocess images for video generation.

**Input**: `multipart/form-data`
- `images`: List of product image files

**Output**: JSON
```json
{
  "results": [
    {
      "index": 0,
      "prediction": "front",
      "image_size": 123456,
      "image_base64": "..."
    }
  ],
  "video_gen_status": "full_rotation",
  "num_images": 3
}
```

### `POST /generate-prompt-r2v`

Generate a Veo prompt from images.

**Input**: `multipart/form-data`
- `all_images`: List of preprocessed image files

**Output**: JSON
```json
{
  "prompt": "A pristine white running shoe rotates slowly...",
  "reference_images": [...],
  "num_reference_images": 3
}
```

### `POST /generate-video-r2v`

Generate a single video clip.

**Input**: `multipart/form-data`
- `reference_images`: List of reference image files
- `index`: Clip index (default: 0)
- `prompt`: Video generation prompt (optional, auto-generated if not provided)
- `reference_type`: Reference type for Veo (default: "asset")
- `max_retries`: Maximum validation retries (default: 4)

**Output**: `video/mp4`

### `POST /run-pipeline-r2v`

Run the complete end-to-end pipeline.

**Input**: JSON
```json
{
  "images_base64": ["base64_image_1", "base64_image_2", ...],
  "max_retries": 4,
  "veo_model": "veo-3.1-generate-001",
  "reference_type": "asset",
  "upscale_images": true,
  "product_id": "shoe_123",
  "gcs_bucket": "my-bucket",
  "gcs_destination_prefix": "shoe_spinning_outputs"
}
```

**Output**: JSON (with GCS)
```json
{
  "video_gen_status": "full_rotation",
  "num_clips": 1,
  "clips": [...],
  "gcs_uris": ["gs://bucket/path/video.mp4", ...],
  "gcs_base_uri": "gs://bucket/prefix/product_id/"
}
```

**Output**: JSON (without GCS)
```json
{
  "video_base64": "...",
  "frames_base64": ["frame1", "frame2", ...],
  "num_frames": 50,
  "retry_count": 1
}
```

### `POST /merge-videos`

Merge multiple video clips with speed adjustments.

**Input**: `multipart/form-data`
- `videos`: List of video files
- `speeds`: JSON array of playback speeds (e.g., `[1.0, 0.5, 1.0]`)

**Output**: `video/mp4`

## Key Components

### `classify_shoes.py`

Classifies shoe images by viewing angle using a custom Vertex AI endpoint.

```python
def classify_shoe(image_bytes, client, shoe_classifier_model) -> str:
    """Returns: 'front', 'back', 'left', 'right', 'top', 'bottom',
    'front_left', 'front_right', 'back_left', 'back_right',
    'multiple', 'invalid'"""
```

### `split_multiple_shoes.py`

Splits images containing multiple shoes into individual images.

```python
def divide_duplicate_image(
    image_bytes, client, return_masks=False
) -> list[bytes]:
    """Returns list of individual shoe image bytes"""
```

### `shoe_images_selection.py`

Selects and orders images for optimal video generation.

```python
def pick_images_by_ordered_best_side(
    images_classified,
) -> list[tuple[bytes, str]]:
    """Returns ordered list of (image_bytes, classification) tuples"""


def classify_video_gen_status(labels) -> str:
    """Returns: 'full_rotation', 'partial_rotation', 'exclude'"""
```

### `video_validation_r2v.py`

Validates generated videos for spin consistency.

```python
def validate_and_fix_product_spin_consistency_r2v(
    video_bytes, client, shoe_classifier_model
):
    """Returns: (is_valid, reason, fixed_video_bytes, frame_classifications, ...)"""
```

### `product_consistency_validation.py`

Validates that the product in the video matches input images.

```python
def validate_product_consistency(video_bytes, frame_classifications, reference_images_bytes, ...):
    """Returns: (is_valid, message, details)"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `REGION` | GCP region for API calls (e.g., `europe-west4`) |
| `SHOE_CLASSIFICATION_ENDPOINT` | Vertex AI endpoint ID for shoe classification |

## Video Generation Status

The pipeline returns a `video_gen_status` indicating coverage:

| Status | Description |
|--------|-------------|
| `full_rotation` | Images cover all major angles (front, back, sides) |
| `partial_rotation` | Some angles missing but video can be generated |
| `exclude` | Insufficient images or invalid angles |

## Usage Example

```python
import base64
import requests

# Load images
images = []
for path in ["front.jpg", "back.jpg", "left.jpg", "right.jpg"]:
    with open(path, "rb") as f:
        images.append(base64.b64encode(f.read()).decode())

# Run pipeline
response = requests.post(
    "http://localhost:8000/api/shoes/spinning/run-pipeline-r2v",
    json={"images_base64": images, "max_retries": 4, "upscale_images": True},
)

result = response.json()
video_bytes = base64.b64decode(result["video_base64"])

with open("spinning_video.mp4", "wb") as f:
    f.write(video_bytes)
```

## Evaluation Tools

The `evaluation_app/` directory contains tools for evaluating video quality:

- **evaluation_viewer**: Web-based viewer for reviewing generated videos and their validation results

## Dependencies

Key Python packages:
- `google-genai`: Vertex AI client
- `opencv-python`: Video/image processing
- `Pillow`: Image manipulation
- `numpy`: Array operations
- `fastapi`: API framework

## Shoe Classifier Training

The shoe position classifier is a fine-tuned Gemini model that classifies shoe images by viewing angle.

Two options:
1. **Baseline local classifier** (no setup needed): Works out of the box when `SHOE_CLASSIFICATION_ENDPOINT=None` in `config.env`. Uses Gemini multimodal embeddings + a numpy neural network.
2. **Fine-tuned Gemini LoRA** (better accuracy): Train a custom classifier using the commands below.

### Training Overview

The classifier identifies 12 different shoe positions:
- `front`, `back`, `left`, `right` - Cardinal directions
- `front_left`, `front_right`, `back_left`, `back_right` - Diagonal views
- `top_front` - Top-down front view
- `sole` - Bottom/sole view
- `multiple` - Multiple shoes in image
- `invalid` - Not a valid shoe image

### Training Notebook

See [`notebooks/train_shoe_classifier.ipynb`](notebooks/train_shoe_classifier.ipynb) for a complete end-to-end training pipeline that:

1. **Loads training data** from a Parquet file with columns:
   - `uri_path`: GCS URI to the image
   - `soft_label`: Classification label
   - `product_id`: Product identifier (for stratified splitting)

2. **Creates train/validation split** using stratified group k-fold to:
   - Maintain class balance
   - Prevent data leakage (same product never in both sets)

3. **Fine-tunes Gemini** using Vertex AI with configurable:
   - Number of epochs (default: 10)
   - LoRA rank (2, 4, or 8)
   - Learning rate multiplier

4. **Evaluates the model** with:
   - Accuracy, precision, recall, F1 scores
   - Confusion matrices (raw counts and percentages)
   - Error analysis HTML report

5. **Automatically selects the best model** based on F1 score (configurable)

### Quick Start

```bash
# Fine-tune Gemini with LoRA
make train-shoe-model

# Evaluate all endpoints and auto-update config.env with the best one
make eval-set-shoe-model
```

Training parameters are configurable via environment variables in `config.env`:
- `FINETUNE_EPOCHS` (default: 10)
- `FINETUNE_LORA_RANK` (default: 2, options: 2, 4, 8)
- `FINETUNE_LR_MULTIPLIER` (default: 0.5)
- `FINETUNE_VERSION` (default: 1)
- `FINETUNE_BASE_MODEL` (default: gemini-3.6-flash)

### Training Data Requirements

| Column | Type | Description |
|--------|------|-------------|
| `uri_path` | string | GCS URI to the image (e.g., `gs://bucket/image.jpg`) |
| `soft_label` | string | Classification label (one of the 12 classes) |
| `product_id` | string | Product identifier for grouping |

A sample dataset (`shoes_classifier_dataset_sample.parquet`) with 1000 stratified examples is included in the notebooks directory for testing.

Recommended minimum: 100 images per class for good performance.

### Hyperparameter Guidelines

| Parameter | Recommended | Notes |
|-----------|-------------|-------|
| `epochs` | 10-20 | Start with 10, increase if underfitting |
| `lora_rank` | 2 | Lower = faster, higher = more capacity |
| `lr_multiplier` | 0.5 | Reduce if training is unstable |

## Troubleshooting

### "Cannot generate video with these images"
- Ensure images cover at least 2 different viewing angles
- Check that images are not classified as `invalid` or `multiple`

### Validation failures after max retries
- The shoe may have features that cause inconsistent spinning
- Try providing clearer, higher-resolution input images
- Consider disabling product consistency validation for testing

### Slow performance
- Image upscaling is the slowest step; set `upscale_images: false` for faster testing
- Reduce `max_retries` if validation is taking too long

### Classifier not working
- Ensure `SHOE_CLASSIFICATION_ENDPOINT` is set in `config.env`
- Verify the endpoint exists and is deployed in Vertex AI
- Check that the endpoint region matches your `REGION` setting
- See [Shoe Classifier Training](#shoe-classifier-training) to train a new model
