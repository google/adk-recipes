# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .artifact_utils import (
    get_artifact_as_local_path,
    get_gcs_file_as_local_path,
    list_available_artifacts,
    read_file_content,
    save_deck_spec,
    save_presentation,
    update_slide_in_spec,
)
from .pptx_editor import (
    _insert_visual_into_slide,
    add_slide_to_end,
    delete_slide,
    edit_slide_text,
    extract_images_from_slide,
    extract_slide_content,
    get_safe_layout,
    inspect_template_style,
    read_presentation_details,
    read_presentation_outline,
    replace_slide_visual,
    update_element_layout,
)
from .presentation_orchestrator import (
    generate_and_render_deck,
    render_deck_from_spec,
)
from .visual_generator import generate_visual

__all__ = [
    "add_slide_to_end",
    "delete_slide",
    "edit_slide_text",
    "extract_images_from_slide",
    "extract_slide_content",
    "generate_and_render_deck",
    "generate_visual",
    "get_artifact_as_local_path",
    "get_gcs_file_as_local_path",
    "inspect_template_style",
    "list_available_artifacts",
    "read_file_content",
    "read_presentation_details",
    "read_presentation_outline",
    "render_deck_from_spec",
    "replace_slide_visual",
    "save_deck_spec",
    "save_presentation",
    "update_element_layout",
    "update_slide_in_spec",
]

from google.adk.tools import FunctionTool

CORE_TOOLS = [
    FunctionTool(func=list_available_artifacts),
    FunctionTool(func=get_artifact_as_local_path),
    FunctionTool(func=inspect_template_style),
    FunctionTool(func=save_deck_spec),
    FunctionTool(func=update_slide_in_spec),
    FunctionTool(func=generate_and_render_deck),
    FunctionTool(func=get_gcs_file_as_local_path),
    FunctionTool(func=save_presentation),
    FunctionTool(func=read_file_content),
]

EDITING_TOOLS = [
    FunctionTool(func=add_slide_to_end),
    FunctionTool(func=delete_slide),
    FunctionTool(func=edit_slide_text),
    FunctionTool(func=replace_slide_visual),
    FunctionTool(func=update_element_layout),
    FunctionTool(func=read_presentation_outline),
    FunctionTool(func=read_presentation_details),
    FunctionTool(func=extract_slide_content),
    FunctionTool(func=extract_images_from_slide),
    FunctionTool(func=generate_visual),
]

ALL_STANDARD_TOOLS = CORE_TOOLS + EDITING_TOOLS
