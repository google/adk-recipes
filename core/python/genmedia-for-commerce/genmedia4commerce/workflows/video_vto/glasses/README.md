# Glasses / Video VTO

> **MCP Tools**: `run_glasses_video_generate`, `run_glasses_video_regenerate` | **ADK Agent**: Routes via Router Agent

This directory contains the video-specific components of the glasses VTO pipeline. For the full overview covering both image and video glasses VTO, see the shared README:

**[genmedia4commerce/workflows/image_vto/glasses/README.md](../../image_vto/glasses/README.md)**

## Video-Specific Files

```
genmedia4commerce/workflows/video_vto/glasses/
├── pipeline.py              # Video generation and regeneration orchestration
├── generate_video_util.py   # Veo video generation, collage creation, post-processing
├── glasses_eval.py          # Video glitch detection (Gemini) and color/face detection
├── custom_template.py       # AI-powered prompt generation for glasses commercials
├── men_templates.jsonl      # Pre-defined male model video templates
├── women_templates.jsonl    # Pre-defined female model video templates
└── videos/                  # Template videos
    ├── men/
    └── women/

genmedia4commerce/mcp_server/video_vto/glasses/
├── glasses_mcp.py           # MCP tools: run_glasses_video_generate, run_glasses_video_regenerate
└── glasses_api.py           # REST API router
```

## Key Differences from Image Pipeline

- **`pipeline.py`**: Orchestrates the full video flow -- background removal, collage creation, Veo generation, post-processing (green screen trimming, glitch detection). Supports both initial generation and regeneration from existing collage data.
- **`generate_video_util.py`**: Creates input collages for Veo (1-3 images on a colored background), generates videos with batching support (>4 videos split into parallel batches), and trims green screen frames from output.
- **`glasses_eval.py`**: Uses Gemini 3.1 Pro to detect video glitches and production artifacts. Also includes OpenCV-based color detection for green screen removal and Vision API face detection for single-person validation.
- **`custom_template.py`**: Uses Gemini to generate structured prompts for glasses commercials from natural language input, including subject, action, scene, camera, and lighting fields. Also generates enhanced animation prompts.
