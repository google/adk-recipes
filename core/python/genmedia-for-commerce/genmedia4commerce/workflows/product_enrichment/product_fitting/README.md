# Product Fitting (Catalogue Enrichment)

> **MCP Tool**: `run_product_fitting` | **ADK Agent**: Routes via Router Agent

Product fitting is a **B2B catalogue enrichment** tool: it takes a single garment's product photos and generates realistic images of that garment worn on an AI-generated model body, producing both front and back views with automated quality evaluation and iterative refinement.

**Product Fitting vs Virtual Try-On (VTO):**
- **Product Fitting** (this workflow): B2B — showcases **one product** on a **preset AI model** for catalogue imagery
- **Virtual Try-On**: B2C — dresses a **real person** in **one or more garments** using their own photo

## Overview

The product fitting pipeline uses a multi-step approach:

1. **Classification**: Classify garment category (top, bottom, dress, footwear, head accessory) and identify front/back views using Gemini
2. **Image Selection**: Select the best 2 images per view based on angle quality and product visibility
3. **Description**: Generate detailed garment descriptions including logos, text, construction details
4. **Generation**: Generate fitting images using Nano Banana with framing-adapted prompts
5. **Evaluation**: Evaluate garment accuracy (Gemini vision) and wearing quality in parallel
6. **Iterative Fix**: If quality is below threshold, fix issues using multi-turn conversation with eval feedback

The pipeline uses:
- **Gemini**: Garment classification, description, and evaluation (garment accuracy + wearing quality)
- **Nano Banana**: Image generation and iterative fix attempts
- **Pre-set Model Photos**: Built-in model images organized by ethnicity and gender

## Directory Structure

```
genmedia4commerce/workflows/product_enrichment/product_fitting/
├── pipeline.py          # Main pipeline orchestration (classify, generate, evaluate, fix)
├── classification.py    # Garment classification, view selection, detailed description
├── generation.py        # Fitting image generation with framing-adapted prompts
├── garment_eval.py      # Garment accuracy and wearing quality evaluation
└── models/              # Pre-set model photos by ethnicity and gender
    ├── african_man/
    ├── african_woman/
    ├── asian_man/
    ├── asian_woman/
    ├── european_boy/
    ├── european_girl/
    ├── european_man/
    ├── european_man2/
    ├── european_woman/
    └── european_woman2/

genmedia4commerce/mcp_server/product_enrichment/product_fitting/
├── product_fitting_mcp.py   # MCP tool: run_product_fitting
└── product_fitting_api.py   # REST API router
```

## Pipeline Overview

```
Garment Images -> Classify -> Select Best -> Describe -> Generate Front -> Evaluate -> Fix -> Generate Back -> Evaluate -> Fix
```

### Stage 1: Classification

A single Gemini call classifies all input images:

- **Category**: `top`, `full_body_outer`, `bottom`, `dress`, `footwear`, `head_accessory`
- **View per image**: `front`, `back`, `other` (using garment construction cues like closures, pockets, neckline)
- **Framing**: Determined from category -- `full_body`, `upper_body`, `lower_body`, `head`, `footwear`

### Stage 2: Image Selection

For each view (front/back), selects the best 2 images ranked by:
- **Angle**: "perfect" (dead-on, straight) vs "angled" (rotated, tilted)
- **Quality**: 1-10 based on clarity, resolution, clean background

For footwear and head accessories, all images are candidates (no view classification).

### Stage 3: Garment Description

Generates a detailed description using all selected images together:
- Product type, material, color scheme, brand identification
- Per-view exterior/interior details: logos, text, construction features
- Cross-references front and back views for symmetric branding

### Stage 4: Generation with Iterative Fix

For each view (front first, then back from best front):

1. **Generate**: Create fitting image with Nano Banana using framing-adapted prompts
2. **Evaluate**: Run garment accuracy eval (0-10 scale) and wearing quality eval (0-3 scale) in parallel
3. **Fix attempt**: If score < 100, append eval feedback and regenerate (multi-turn conversation)
4. **Keep best**: Track the best result across original and fix attempts
5. **Repeat**: Up to `max_retries` attempts per view
6. **Final fix**: If best score < 90 after all attempts, try one last fix on the overall best

A result is "discarded" if garment score < 7 or wearing quality score <= 1.

## Key Components

### `classification.py`

```python
def classify_garments(client, garment_images_bytes_list) -> dict:
    """Classify garment category and view for each image.
    Returns: {category, description, views: [{index, view}]}"""


def get_framing(category: str) -> str:
    """Map category to framing: full_body, upper_body, lower_body, head, footwear"""


def select_best_front(client, garment_images) -> dict:
    """Select up to 2 best front-view images by angle quality"""


def select_best_back(client, garment_images) -> dict:
    """Select up to 2 best back-view images by angle quality"""


def describe_garment_detailed(client, front_images, back_images) -> dict:
    """Generate detailed description with exterior/interior details.
    Returns: {general, front_details, back_details}"""
```

### `generation.py`

```python
def generate_fitting(client, scenario, garment_images, preprocessed_model, ...) -> tuple:
    """Generate a product fitting image with framing-adapted prompts.
    Returns: (image_bytes, user_message, config) or (None, None, None)"""

def generate_fitting_back_from_front(client, scenario, back_garment_images, best_front_image, ...) -> tuple:
    """Generate back view from best front result. Same person, turned around."""

def fix_fitting(client, original_message, original_config, generated_image, eval_feedback, ...) -> bytes:
    """Fix a generated image using multi-turn conversation with eval feedback."""
```

### `garment_eval.py`

```python
def evaluate_garments(client, generated_image_bytes, garment_images_bytes_list, ...) -> dict:
    """Evaluate garment reproduction accuracy (0-100 scale).
    Returns: {discard: bool, garments_score: float, garment_details: list}"""

def evaluate_footwear(client, generated_image_bytes, reference_images, ...) -> dict:
    """Simplified evaluation for footwear products."""

def evaluate_wearing_quality(client, generated_image_bytes, ...) -> dict:
    """Evaluate how naturally the outfit is worn (0-3 scale).
    Returns: {explanation: str, score: int}"""
```

### `pipeline.py`

```python
async def run_fitting_pipeline(
    garment_images_bytes, model_photo_map, max_retries,
    scenario, generation_model, gender, nano_client, genai_client, ...
) -> dict:
    """Run the full pipeline: classify -> describe -> generate with retries.
    Returns: {front, back, classification, framing, sse_events}"""
```

## Configuration

Environment variables (in `config.env`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ID` | Google Cloud project ID |
| `NANO_LOCATION` | Nano Banana API location (default: "global") |

### MCP Tool Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `garment_images_base64` | Yes | -- | List of base64-encoded garment product images |
| `gender` | Yes | -- | Model gender: `man`, `woman` |
| `ethnicity` | No | `european` | Model preset: `african`, `asian`, `european` |
| `scenario` | No | Pure white background | Background description |
| `max_retries` | No | 3 | Maximum generation attempts per view |
| `generation_model` | No | `gemini-3.1-flash-image` | Gemini model for generation |
| `product_id` | No | -- | Optional product identifier for logging |
| `model_photos` | No | -- | Custom model photos (overrides ethnicity preset) |

## Model Photo Presets

Pre-set model photos are stored in the `models/` directory. Each preset folder contains `front_top.png` and `front_bottom.png` images. Available presets:

- `african_man`, `african_woman`
- `asian_man`, `asian_woman`
- `european_man`, `european_man2`, `european_woman`, `european_woman2`
- `european_boy`, `european_girl`

Custom model photos can be provided via the `model_photos` parameter to override presets.

## Scoring

### Garment Accuracy (0-10)

| Score | Quality |
|-------|---------|
| 9-10 | Near-perfect or perfect reproduction |
| 8 | Very good -- correct color, pattern, logos, minor differences |
| 7 | Good -- correct overall design, noticeable construction differences |
| 5-6 | Notable flaws -- missing exterior logos or wrong color/pattern |
| 0-4 | Major imperfections or garment missing |

Hard rules: missing exterior logos cap at 6, wrong position caps at 6, duplicated logos cap at 7, visible interior elements cap at 6.

### Wearing Quality (0-3)

| Score | Quality |
|-------|---------|
| 3 | Excellent -- could pass as real e-commerce photo |
| 2 | Acceptable -- minor imperfections on close inspection |
| 1 | Poor -- significant issues (half-tucked, major clipping, product obscured) |
| 0 | Unwearable -- garment broken or product hidden |

## Troubleshooting

### "No valid garment images found"
- Ensure images show a single garment not worn by a person
- Provide front-facing product photos for best results

### Low garment scores
- The pipeline retries up to `max_retries` times with fix attempts
- Provide clear, high-resolution product images from straight-on angles
- Include both front and back views for best results

### Back view not generated
- Back generation requires a successful front result to use as reference
- Provide back-facing product images for back view generation
