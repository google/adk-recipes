# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Debug utilities for saving intermediate images during processing.

Set environment variable DEBUG_IMAGES=1 to enable debug image saving.
Images are saved to src/shared/debug/{timestamp}/
"""

import io
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Debug folder path - relative to this file
DEBUG_FOLDER = Path(__file__).parent / "debug"

# Global flag to enable/disable debug saving
# Set DEBUG_IMAGES=1 environment variable to enable
DEBUG_ENABLED = False

if DEBUG_ENABLED:
    logger.info("[Debug] Debug image saving is ENABLED (DEBUG_IMAGES=1)")


def get_debug_session_folder():
    """Get or create a timestamped folder for this debug session."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_folder = DEBUG_FOLDER / timestamp
    session_folder.mkdir(parents=True, exist_ok=True)
    return session_folder


# Session folder created once per import
_session_folder = None


def _get_session_folder():
    """Lazy initialization of session folder."""
    global _session_folder
    if _session_folder is None:
        _session_folder = get_debug_session_folder()
        logger.info(f"[Debug] Saving images to: {_session_folder}")
    return _session_folder


def get_current_session_key():
    """Get the name of the current debug session folder."""
    folder = _get_session_folder()
    return folder.name


def save_debug_image(img_bytes, step_name, prefix=""):
    """Save an image to the debug folder with a descriptive name.

    Args:
        img_bytes: Image as bytes
        step_name: Name of the processing step (e.g., "01_original", "02_cropped")
        prefix: Optional prefix for grouping (e.g., "face", "body", "eval")

    Returns:
        Path to saved file, or None if saving failed

    """
    if not DEBUG_ENABLED:
        return None

    try:
        session_folder = _get_session_folder()

        # Build filename
        if prefix:
            filename = f"{prefix}_{step_name}.png"
        else:
            filename = f"{step_name}.png"

        filepath = session_folder / filename

        # Save the image with EXIF orientation applied
        img = Image.open(io.BytesIO(img_bytes))
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:  # noqa: S110
            pass  # Ignore EXIF errors
        img.save(filepath, format="PNG")

        logger.info(f"[Debug] Saved: {filename} ({img.width}x{img.height})")
        return filepath

    except Exception as e:
        logger.warning(f"[Debug] Failed to save {step_name}: {e}")
        return None


def reset_session():
    """Reset the session folder to create a new one for the next run."""
    global _session_folder
    _session_folder = None


def set_debug_enabled(enabled: bool):
    """Enable or disable debug image saving."""
    global DEBUG_ENABLED
    DEBUG_ENABLED = enabled
    logger.info(f"[Debug] Debug saving {'enabled' if enabled else 'disabled'}")
