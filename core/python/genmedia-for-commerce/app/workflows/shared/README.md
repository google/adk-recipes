# Shared Utilities

Common utilities shared across all product modules. These provide core functionality for image processing, video manipulation, AI service integration, cloud storage, and parallel execution.

## Overview

| Module | Purpose |
|--------|---------|
| `image_utils.py` | Image processing: upscaling, background removal, cropping, canvas creation |
| `video_utils.py` | Video processing: frame extraction, merging, similarity analysis |
| `veo_utils.py` | Veo video generation: image-to-video, interpolation, R2V |
| `gcs_utils.py` | Google Cloud Storage: upload, download, folder sync |
| `llm_utils.py` | Gemini/LLM helpers: content parts, config, retry logic |
| `person_eval.py` | Face comparison and identity verification |
| `utils.py` | Generic utilities: parallel processing |
| `vector_search.py` | In-memory product catalog search (numpy dot-product) |
| `debug_utils.py` | Debug image saving utilities |

## Module Details

---

### `image_utils.py`

Core image processing operations used throughout the application.

#### Background Removal

```python
def replace_background(
    client,
    img_bytes,
    contour_tolerance=0.01,
    background_color="#FFFFFF",
    mask_margin_pixels=40,
) -> bytes:
    """
    Extract product from background and place on new background.

    Args:
        client: Gemini client for Vertex AI segmentation
        img_bytes: Input image bytes
        contour_tolerance: Margin around bounding box as percentage (default: 0.01)
        background_color: Hex color string or None for transparent (default: "#FFFFFF")
        mask_margin_pixels: Pixels to dilate mask for edge margin (default: 40)

    Returns:
        bytes: Product on specified background as PNG
    """
```

Uses Vertex AI segmentation with fallback to `rembg` library.

#### Image Upscaling

```python
def upscale_image_bytes(client, image_bytes, upscale_factor="x4") -> bytes:
    """
    Upscale image using Imagen 4.0 Upscale API.

    Automatically adjusts factor if result would exceed 17M pixels.
    Factors: "x2", "x3", "x4"

    Returns:
        bytes: Upscaled image, or original if too large
    """
```

#### Face Cropping

```python
def crop_face(img_bytes, padding_ratio=0.3) -> bytes:
    """
    Detect and crop face using Google Cloud Vision API.

    Args:
        img_bytes: Input image bytes
        padding_ratio: Padding around face as percentage (default: 0.3 = 30%)

    Returns:
        bytes: Cropped face as PNG, or None if no face detected
    """
```

#### Canvas Creation

```python
def create_canvas(
    product_image_bytes,
    canvas_width=1920,
    canvas_height=1080,
    margin_top=150,
    margin_side=0,
    bg_color=(255, 255, 255, 255),
    zoom_factor=1.0,
    target_diagonal=None,
    target_height=None,
) -> bytes:
    """
    Center product on a canvas with configurable sizing.

    Scaling modes:
    - target_diagonal: Scale to specific diagonal (best for rotation videos)
    - target_height: Scale to specific height
    - Default: Fit within margins
    """


def create_canvas_with_height_scaling(
    images_bytes,
    canvas_height=1080,
    canvas_width=1920,
    margin_top=60,
    margin_side=300,
) -> list[bytes]:
    """
    Create canvases for multiple images with consistent height.
    Calculates shared target height that fits all images.
    """
```

#### Image Stacking

```python
def stack_images_horizontally(img1_bytes, img2_bytes, padding=0.03) -> bytes:
    """Stack two images side by side with optional padding."""


def stack_and_canvas_images(
    images, classes=None, canvas_height=2160, canvas_width=3840
) -> list[bytes] | tuple:
    """
    Create 4K canvases, stacking last two images if 4 provided.

    For 1-3 images: Individual canvases
    For 4 images: Stack images 3+4, return 3 canvases
    """
```

#### Combined Operations

```python
def extract_upscale_product(
    client,
    upscale_client,
    img_bytes,
    clean_after_upscale=True,
    mask_margin_pixels=40,
) -> bytes:
    """
    Full pipeline: extract product → upscale → clean artifacts.

    Args:
        clean_after_upscale: Re-apply mask after upscaling to remove artifacts
        mask_margin_pixels: Edge margin to preserve during cleanup
    """
```

#### Batch Preprocessing

```python
def preprocess_images(
    images_bytes_list,
    client,
    upscale_client,
    num_workers=16,
    upscale_images=True,
    create_canva=True,
) -> list[bytes]:
    """
    Preprocess images with optional upscaling and canvas creation.

    Used by both Interpolation and R2V modes for video generation.

    Args:
        images_bytes_list: List of image bytes to preprocess
        client: Gemini client for background removal
        upscale_client: Client for image upscaling
        num_workers: Number of parallel workers (default: 16)
        upscale_images: If True, extract and upscale products (default: True)
        create_canva: If True, create canvas with consistent sizing (default: True)

    Returns:
        list[bytes]: List of preprocessed image bytes
    """
```

Usage example:

```python
from workflows.shared.image_utils import preprocess_images

# Preprocess images for video generation
processed = preprocess_images(
    images_bytes_list=raw_images,
    client=gemini_client,
    upscale_client=imagen_client,
    upscale_images=True,
    create_canva=True,
)
```

---

### `video_utils.py`

Video processing and analysis utilities.

#### Frame Extraction

```python
def extract_frames_as_bytes_list(
    video_bytes, image_format=".png"
) -> list[bytes]:
    """Extract all frames from video as list of image bytes."""
```

#### Video Creation

```python
def create_mp4_from_bytes_to_bytes(frames_bytes, fps=24, quality=7) -> bytes:
    """Create MP4 video in memory from list of frame bytes."""


def reverse_video(video_bytes, fps=24, quality=7) -> bytes:
    """Reverse a video by reversing frame order."""
```

#### Video Merging

```python
def merge_videos_from_bytes(videos_bytes, speeds=None, fps=24) -> bytes:
    """
    Merge multiple videos with optional speed adjustments.

    Args:
        videos_bytes: List of video bytes to merge
        speeds: Optional list of speed multipliers (e.g., [1.0, 0.5, 2.0])
        fps: Output frames per second

    Returns:
        bytes: Merged video
    """
```

#### Frame Similarity

```python
def get_frame_similarity_bytes(frame1_bytes, frame2_bytes) -> float:
    """Calculate SSIM similarity between two images (0-1 scale)."""


def find_most_similar_frame_index(
    all_frames, reference_frame, num_frames_to_check=None
) -> int:
    """Find index of frame most similar to reference."""
```

#### Frame Conversion

```python
def convert_image_to_video_frame(video_frame_bytes, image_bytes) -> bytes:
    """Resize and center-crop image to match video frame dimensions."""
```

---

### `veo_utils.py`

Veo video generation wrappers with automatic retry.

#### Image-to-Video / Interpolation

```python
def generate_veo(
    client,
    image,
    prompt,
    last_frame=None,
    model="veo-3.1-fast-generate-001",
    duration=8,
    number_of_videos=1,
    aspect_ratio="16:9",
    person_generation=None,
    enhance_prompt=None,
    generate_audio=False,
) -> list[bytes]:
    """
    Generate video from starting image.

    Modes:
    - Image-to-video: Only provide `image`
    - Interpolation: Provide `image` and `last_frame`

    Returns:
        list[bytes]: Generated videos, or empty list on failure
    """
```

#### Reference-to-Video (R2V)

```python
def generate_veo_r2v(
    client,
    reference_images,
    prompt,
    reference_type="asset",
    model="veo-3.1-fast-generate-001",
    duration=8,
    generate_audio=False,
) -> bytes:
    """
    Generate video using reference images.

    Args:
        reference_images: List of reference image bytes (max 3)
        reference_type: "asset" or "style"

    Returns:
        bytes: Generated video, or None on failure
    """
```

Both functions use `@retry_with_exponential_backoff` for automatic retry on failures.

---

### `gcs_utils.py`

Google Cloud Storage operations using Transfer Manager for high-performance uploads.

#### Single File Operations

```python
def upload_file_to_gcs(
    bucket_name,
    source_file_path,
    destination_blob_name,
    project_id=None,
    content_type=None,
) -> str:
    """Upload single file, returns GCS URI."""


def upload_bytes_to_gcs(
    bucket_name,
    file_bytes,
    destination_blob_name,
    project_id=None,
    content_type=None,
) -> str:
    """Upload bytes directly, returns GCS URI."""


def download_file_from_gcs(
    bucket_name, source_blob_name, destination_file_path, project_id=None
) -> str:
    """Download file from GCS."""
```

#### Folder Upload

```python
def upload_folder_to_gcs(
    bucket_name,
    source_folder_path,
    destination_prefix="",
    project_id=None,
    include_extensions=None,
    exclude_extensions=None,
    max_workers=50,
) -> list[str]:
    """
    Upload entire folder using parallel Transfer Manager.

    Args:
        source_folder_path: Local folder to upload
        destination_prefix: GCS prefix (e.g., "outputs/product_123")
        include_extensions: Filter to specific extensions (e.g., ['.jpg', '.png'])
        exclude_extensions: Exclude specific extensions
        max_workers: Parallel upload workers (default: 50)

    Returns:
        list[str]: List of uploaded GCS URIs
    """
```

#### Pipeline Results Upload

```python
def save_and_upload_to_gcs(
    result,
    product_id,
    bucket_name,
    gcs_destination_prefix="shoe_spinning_outputs",
    project_id=None,
    pre_sampled_frames=None,
    image_format="png",
) -> list[str]:
    """
    Orchestrate saving and uploading video pipeline results.

    Handles:
    - Final video + frames
    - Per-clip videos, reference images, metadata
    - Manifest file with all URIs

    Args:
        result: Pipeline result dict with video_bytes, clips, etc.
        product_id: Product identifier
        pre_sampled_frames: Required - pre-extracted frame bytes

    Returns:
        list[str]: All uploaded file URIs
    """
```

---

### `llm_utils.py`

Gemini/LLM integration utilities.

#### Retry Decorator

```python
@retry_with_exponential_backoff(
    max_retries=5,
    initial_delay=1.0,
    exponential_base=5.0,
    max_delay=60.0,
    exceptions=(ClientError,),
)
def some_function():
    """Automatically retries on failure with exponential backoff."""
```

Delay progression: 1s → 5s → 25s → 60s → 60s

#### Content Part Creation

```python
def get_part(input_piece, return_dict=False) -> Part:
    """
    Convert input to Gemini Part type.

    Handles:
    - bytes: Auto-detects image/video MIME type
    - "gs://..." string: GCS path with auto MIME detection
    - Other strings: Text content
    """


def get_mime_type_from_bytes(data) -> str:
    """Detect MIME type from file signature (magic bytes)."""


def get_mime_type_from_path(path) -> str:
    """Detect MIME type from file extension."""
```

#### Generation Config

```python
def get_generate_content_config(
    temperature=1,
    top_p=0.95,
    max_output_tokens=32768,
    response_modalities=None,
    response_mime_type=None,
    response_schema=None,
    system_instruction=None,
    thinking_budget=None,
    safety_off=True,
    image_config=None,
) -> GenerateContentConfig:
    """
    Create standard Gemini generation config.

    Args:
        response_modalities: ["TEXT"], ["IMAGE"], ["IMAGE", "TEXT"]
        response_mime_type: "application/json", "text/plain"
        response_schema: Dict for structured JSON output
        thinking_budget: Token budget for reasoning
        safety_off: Disable all safety filters (default: True)
        image_config: Dict with aspect_ratio, image_size, output_mime_type
    """
```

---

### `person_eval.py`

Face comparison for VTO and background changer quality evaluation.

#### Face Comparison

```python
def compare_faces(
    reference_face_bytes, generated_face_bytes, model_name="ArcFace"
) -> dict:
    """
    Compare two face images using DeepFace.

    Args:
        model_name: "ArcFace" (default, most accurate), "VGG-Face",
                    "Facenet", "Facenet512", "OpenFace", "DeepFace",
                    "DeepID", "Dlib", "SFace", "GhostFaceNet"

    Returns:
        {
            "distance": float,  # Cosine distance (lower = more similar)
            "model": str,
            "similarity_percentage": float  # 0-100 scale
        }
    """
```

#### Person Match Evaluation

```python
def evaluate_person_match(
    reference_face_bytes, generated_vto_bytes, model_name="ArcFace"
) -> dict:
    """
    Main evaluation function for generated images.

    Handles cropping face from generated image and comparing.

    Args:
        reference_face_bytes: Pre-cropped reference face
        generated_vto_bytes: Full generated image

    Returns:
        {
            "similarity_percentage": float,
            "distance": float,
            "model": str,
            "face_detected": bool
        }
    """
```

---

### `utils.py`

Generic parallel processing utilities.

```python
def predict_parallel(
    to_predict, predict_function, max_workers=8, show_progress_bar=True
) -> list:
    """
    Execute function in parallel using ThreadPoolExecutor.

    Args:
        to_predict: Iterable of inputs
        predict_function: Function to apply to each input
        max_workers: Parallel workers (default: 8)
        show_progress_bar: Show tqdm progress bar (default: True)

    Returns:
        list: Results in same order as inputs
    """
```

---

### `vector_search.py`

In-memory product catalog search using pre-computed embeddings. Data is downloaded from GCS at startup and cached locally.

```python
def search(query: str, k: int = 20) -> list[dict]:
    """Search catalog by text query. Embeds the query and returns top-k products."""


def search_by_vector(embedding: list[float], k: int = 20) -> list[dict]:
    """Search catalog by pre-computed embedding vector (numpy dot-product)."""


def embed_query(text: str) -> list[float]:
    """Embed a text query using Gemini embedding model."""


def search_for_outfit_item(item: dict, k: int = 20) -> dict:
    """Search for a single outfit item and enrich with matched products."""
```

- **61,786 products**, ~42ms per search
- Embeddings: `embeddings.npy` (float32, 724 MB), Metadata: `metadata.parquet` (23 MB)
- Loaded eagerly at import time via `_load()` at module level
- Used by Style Advisor agent and `catalog_search` MCP tool

---

### `debug_utils.py`

Debug image saving utilities for development and troubleshooting.

```python
def save_debug_image(image_bytes, name, prefix="debug") -> str:
    """Save image to debug folder with timestamp for inspection."""
```

---

## Usage Examples

### Background Removal + Upscaling

```python
from workflows.shared.image_utils import extract_upscale_product

# Full pipeline: extract → upscale → clean
processed = extract_upscale_product(
    client=gemini_client,
    upscale_client=imagen_client,
    img_bytes=product_image,
    clean_after_upscale=True,
)
```

### Video Generation with R2V

```python
from workflows.shared.veo_utils import generate_veo_r2v

video_bytes = generate_veo_r2v(
    client=veo_client,
    reference_images=[img1_bytes, img2_bytes, img3_bytes],
    prompt="A sleek product rotates 360 degrees on white background",
    reference_type="asset",
)
```

### Parallel Image Processing

```python
from workflows.shared.utils import predict_parallel
from workflows.shared.image_utils import replace_background

# Process multiple images in parallel
processed_images = predict_parallel(
    to_predict=image_list,
    predict_function=lambda img: replace_background(client, img),
    max_workers=16,
)
```

### Face Evaluation

```python
from workflows.shared.person_eval import evaluate_person_match

result = evaluate_person_match(
    reference_face_bytes=cropped_face, generated_vto_bytes=generated_image
)

if result["face_detected"] and result["similarity_percentage"] > 85:
    print("Good match!")
```

### GCS Upload

```python
from workflows.shared.gcs_utils import upload_folder_to_gcs

uploaded = upload_folder_to_gcs(
    bucket_name="my-bucket",
    source_folder_path="/tmp/outputs",
    destination_prefix="products/shoe_123",
    max_workers=50,
)
```

---

## Testing

Unit tests are located in `tests/workflows/` at the project root, mirroring the `workflows/` structure.

```bash
# Run all tests
make test

# Run tests with coverage report
make test-cov
```

### Test Structure

```
tests/
├── conftest.py                              # Shared fixtures
├── unit/                                    # Unit tests
│   ├── test_router_agent.py
│   ├── test_mcp_product_fitting.py
│   └── test_mcp_catalog.py
└── workflows/                               # Workflow tests
    ├── shared/
    │   ├── test_utils.py
    │   ├── test_llm_utils.py
    │   ├── test_image_utils.py
    │   ├── test_video_utils.py
    │   ├── test_veo_utils.py
    │   ├── test_gcs_utils.py
    │   ├── test_person_eval.py
    │   ├── test_nano_banana.py
    │   ├── test_vector_search.py
    │   └── test_debug_utils.py
    ├── image_vto/clothes/
    ├── image_vto/glasses/
    ├── video_vto/glasses/
    │   └── test_glasses_eval.py
    └── spinning/
        ├── r2v/shoes/
        │   ├── test_classify_shoes.py
        │   ├── test_split_multiple_shoes.py
        │   ├── test_product_consistency_validation.py
        │   ├── test_shoe_images_selection.py
        │   ├── test_video_validation_r2v.py
        │   └── test_images_utils.py
        └── interpolation/other/
            └── test_interpolation_utils.py
```

### Test Coverage

| Module | Tested Functions |
|--------|------------------|
| `workflows/shared/utils.py` | `predict_parallel` |
| `workflows/shared/llm_utils.py` | MIME detection, part creation, config |
| `workflows/shared/image_utils.py` | Canvas creation, image stacking |
| `workflows/shared/video_utils.py` | Frame similarity, video creation, extraction |
| `workflows/shared/debug_utils.py` | Debug image saving utilities |
| `workflows/spinning/r2v/shoes/split_multiple_shoes.py` | Mask operations, image construction |
| `workflows/spinning/r2v/shoes/video_validation_r2v.py` | Path validation, frame sampling |
| `workflows/spinning/r2v/shoes/shoe_images_selection.py` | View selection, pixel counting |
| `workflows/video_vto/glasses/glasses_eval.py` | Color detection, face counting |
| `workflows/spinning/interpolation/other/interpolation_utils.py` | Prompt generation, video post-processing |

Tests focus on pure functions. Functions requiring external API calls (Gemini, Veo, GCS, DeepFace) are not unit tested.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `google-genai` | Vertex AI client (Gemini, Veo, Nano Banana, Imagen) |
| `google-cloud-vision` | Face detection |
| `google-cloud-storage` | GCS operations |
| `opencv-python` | Image/video processing |
| `Pillow` | Image manipulation |
| `numpy` | Array operations |
| `rembg` | Fallback background removal |
| `deepface` | Face comparison |
| `scikit-image` | SSIM calculation |
| `moviepy` | Video merging with speed control |
| `imageio` | Video I/O |
| `tqdm` | Progress bars |
