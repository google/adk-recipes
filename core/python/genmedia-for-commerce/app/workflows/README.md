# Workflows

Each workflow has three layers:

1. **Pipeline** (`workflows/*/pipeline.py`) — core business logic
2. **MCP wrapper** (`mcp_server/*_mcp.py`) — exposes pipeline as MCP tool for the agent
3. **REST API** (`mcp_server/*_api.py`) — FastAPI endpoint for the frontend

## Image VTO

Virtual try-on images — a real person wearing garments or glasses.

| Variant | REST Endpoint | Pipeline |
|---------|--------------|----------|
| Clothes | `POST /api/clothes/generate-vto` | `image_vto/clothes/pipeline.py` |
| Glasses | `POST /api/glasses/generate-vto` | `image_vto/glasses/pipeline.py` |

**Agent MCP tool**: `image_vto` — unified, routes via `is_glasses` flag.

**Clothes flow**: full body image + garment images → Gemini image generation → face evaluation → best result.

**Glasses flow**: face image + glasses image → Gemini image generation with segmentation → face evaluation.

## Video VTO

Virtual try-on videos — animates VTO result into a catwalk-style video.

| Variant | REST Endpoint | Pipeline |
|---------|--------------|----------|
| Clothes (full) | `POST /api/clothes/video/generate-video-vto` | `video_vto/clothes/pipeline.py → run_video_vto` |
| Clothes (video only) | `POST /api/clothes/video/generate-animate-model` | `video_vto/clothes/pipeline.py → run_animate_model` |
| Glasses | `POST /api/glasses/generate-video` | `video_vto/glasses/pipeline.py` |

**Agent MCP tools**: `video_vto` (full pipeline) and `animate_model` (video-only from ready image).

**Full pipeline** (`run_video_vto`): person image + garments → image VTO → pick best → Veo R2V → face evaluation → filtered videos.

**Animate model** (`run_animate_model`): ready model image → Veo R2V → face evaluation → filtered videos. Called by `run_video_vto` internally, or directly when the image is already prepared.

**Veo R2V**: Generates 3 video framings (lower body → upper body → face) with early-abort retry logic. Face similarity evaluated via InsightFace ArcFace embeddings.

## Product Fitting

B2B catalogue enrichment — garment on AI model body (front + back views).

| REST Endpoint | Pipeline |
|--------------|----------|
| `POST /api/product-enrichment/product-fitting/generate-fitting-pipeline` | `product_enrichment/product_fitting/pipeline.py` |

**Agent MCP tool**: `product_fitting`

## Product Spinning

360° spinning videos from product images.

| Variant | REST Endpoint | Pipeline |
|---------|--------------|----------|
| Shoes (R2V) | `POST /api/shoes/spinning/run-pipeline-r2v` | `spinning/r2v/shoes/pipeline.py` |
| Other (R2V) | `POST /api/spinning/r2v/other/pipeline` | `spinning/r2v/other/pipeline.py` |
| Interpolation | `POST /api/spinning/interpolation/other/generate-all` | `spinning/interpolation/other/pipeline.py` |

**Agent MCP tool**: `product_spinning` — unified, routes via `is_shoes` flag.

**Shoes pipeline**: classifies shoe angles → validates → Veo R2V → upscale → quality check with retries.

**Other R2V**: product images → background removal → Veo R2V.

**Interpolation**: multiple angle images → Veo frame interpolation → merge.

## Background Changer

Replace background while preserving the subject.

| REST Endpoint | Pipeline |
|--------------|----------|
| `POST /api/other/background-changer/change-background` | `other/background_changer/pipeline.py` |

**Agent MCP tool**: `background_changer`

## Catalog Search

Text-based search across 61K+ fashion products using pre-computed Gemini embeddings.

| REST Endpoint | Pipeline |
|--------------|----------|
| `POST /api/catalog/search` | `shared/vector_search.py` |

**Agent MCP tool**: `catalog_search`

## Shared Utilities (`shared/`)

| Module | Purpose |
|--------|---------|
| `image_utils.py` | Image resize, crop face, format conversion |
| `video_utils.py` | Frame extraction, video merging |
| `person_eval.py` | Face similarity (InsightFace), model photo validation |
| `segmentation.py` | Image segmentation via Vertex AI |
| `llm_utils.py` | Gemini text/image generation helpers |
| `gcs_utils.py` | GCS upload/download |
| `vector_search.py` | Embedding-based catalog search |
