# Clothes / Video Virtual Try-On (VTO)

> **MCP Tool**: `run_video_vto_clothes` | **ADK Agent**: Routes via Router Agent

Video virtual try-on is a **B2C consumer-facing** tool: it takes a real person's photo and one or more garment images, generates a realistic VTO image of that person wearing the garments, then animates it into a catwalk-style video using Veo 3.1 R2V. Unlike product fitting (B2B catalogue enrichment with a single product on a preset AI model), VTO supports multiple garments and uses the customer's own photo.

## Overview

The video VTO pipeline combines image VTO with video generation:

1. **Image VTO**: Generate a static try-on image using the image VTO clothes pipeline
2. **Framing**: Split the VTO result into three reference framings (lower body, upper body, face)
3. **Video Generation**: Use Veo 3.1 R2V mode with the three framings as reference images
4. **Face Evaluation**: Validate face similarity across video frames, rank and filter results

The pipeline uses:
- **Nano Banana**: Image VTO generation (via image VTO pipeline)
- **Veo 3.1 R2V**: Reference-to-video generation with multi-image references
- **Face Similarity**: MediaPipe-based face comparison for quality filtering

## Directory Structure

```
genmedia4commerce/workflows/video_vto/clothes/
├── pipeline.py              # Unified video VTO pipeline (async generator)
└── generate_video_util.py   # R2V video generation and framing utilities

genmedia4commerce/mcp_server/video_vto/clothes/
├── clothes_mcp.py           # MCP tool: run_video_vto_clothes
└── clothes_api.py           # REST API router
```

## Pipeline Overview

```
Full Body + Garments → Image VTO → Pick Best → Split Framings → Veo R2V → Face Eval → Filter & Rank
```

### Stage 1: Image VTO

Runs the image VTO clothes pipeline to generate the best static try-on result:

1. Generate multiple VTO variations
2. Evaluate each variation for face similarity and garment accuracy
3. Select the best result by score

This stage can be skipped by providing a `vto_result_image` directly (video-only regeneration).

### Stage 2: Reference Framing

Splits the VTO result into three reference images that match the animation flow:

1. **Lower body** (bottom 60%): Used for the opening sequence (waist-down to shoes)
2. **Upper body** (top 40%): Used as the camera tilts up during the walk
3. **Face** (cropped with padding): Used for the final close-up reveal

Each framing is placed onto a 16:9 canvas for Veo input.

### Stage 3: Video Generation (R2V)

Generates videos using Veo 3.1 reference-to-video mode:

1. Pass three reference images (lower body, upper body, face) as asset references
2. Generate N videos in parallel (1 video per Veo call)
3. **Early-abort logic**: Evaluate the first completed clip for face similarity. If it scores below the threshold (40%), cancel remaining clips and retry (up to 5 retries).

### Stage 4: Face Evaluation

Evaluates face consistency across all generated videos:

1. Sample 8 frames uniformly from each video (starting at 6 seconds, when the face is visible)
2. Compare each frame against the reference face using embedding similarity
3. Compute median similarity score (ignoring frames with no detected face)
4. Rank videos by score, filter out those below 70% threshold

## Key Components

### `pipeline.py`

Unified async generator pipeline.

```python
async def run_video_vto(
    full_body_image: bytes,
    garment_images: list[bytes | str],
    scenario: str = "a plain white studio background",
    num_variations: int = 3,
    face_image: bytes | None = None,
    number_of_videos: int = 4,
    prompt: str = "",
    vto_result_image: bytes | None = None,
) -> AsyncGenerator[dict, None]:
    """Run the unified video VTO pipeline. Yields SSE-friendly dicts."""
```

### `generate_video_util.py`

R2V video generation and framing utilities.

```python
def run_r2v_pipeline(
    veo_client,
    model_image_bytes,
    prompt,
    number_of_videos=4,
    upscale_client=None,
    original_model_image_bytes=None,
    first_clip_check=None,
) -> dict:
    """Run R2V pipeline with Veo 3.1 reference images.
    Returns: {videos, first_frame, last_frame, aborted}"""


DEFAULT_VEO_PROMPT = """
Subject: The exactly same person from the reference image...
Sequence 1 (00:00 - 00:02): Standing still, then walking...
Sequence 2 (00:02 - 00:04): Walking forward, camera tilts up...
Sequence 3 (00:04 - 00:06): Face revealed for the first time...
Sequence 4 (00:06 - 00:08): Stops close to camera, looking into lens...
"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `VEO_LOCATION` | Veo API location (default: "global") |
| `NANO_LOCATION` | Nano Banana API location (default: "global") |

## Pipeline Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `EVAL_SAMPLE_COUNT` | 8 | Frames sampled per video for face evaluation |
| `MIN_SIMILARITY_THRESHOLD` | 70.0% | Minimum face similarity to include a video |
| `EARLY_ABORT_THRESHOLD` | 40.0% | Minimum first-clip score before retrying |
| `MAX_RETRIES` | 5 | Maximum retry attempts for early-abort |

## SSE Event Flow

The pipeline yields events in this order:

1. `{"status": "generating_image"}` -- image VTO starting (skipped if vto_result_image provided)
2. `{"status": "image_ready", "image_base64": "...", "final_score": ...}` -- best VTO image selected
3. `{"status": "generating_videos"}` -- R2V video generation starting
4. `{"status": "videos", "videos": [...], "scores": [...], "filenames": [...]}` -- final results
   OR `{"status": "error", "detail": "..."}` -- pipeline failure

## Troubleshooting

### "All generated videos failed face similarity check"
- The model's face may not be clearly visible in the VTO result
- Try using a clearer face photo with the `face_image` parameter
- The early-abort retry logic will attempt up to 5 regeneration cycles

### Videos rejected by face evaluation
- Videos are evaluated from frame 6 seconds onward (the face is not visible earlier)
- Only videos scoring above 70% median face similarity are returned
- Generate more videos (`number_of_videos`) to increase the chance of passing

### Model photo validation failed
- The pipeline validates that the model photo has a visible face, looking at the camera with eyes open
- Use a front-facing portrait photo with clear visibility
