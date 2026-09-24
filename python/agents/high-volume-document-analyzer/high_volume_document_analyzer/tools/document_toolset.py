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

# high_volume_document_analyzer/tools/document_toolset.py

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

import google.auth
from google.adk.tools import ToolContext

from .process_toolset import download_batch_async, fetch_document_urls_async

if TYPE_CHECKING:
    from vertexai.generative_models import GenerativeModel

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

CHUNK_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MODEL_NAME = os.getenv("MODEL_NAME_DOC_PROCESSING", "gemini-2.5-flash")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

_MODEL_INSTANCE: "GenerativeModel | None" = None


def get_model_instance() -> "GenerativeModel":
    global _MODEL_INSTANCE
    if _MODEL_INSTANCE is None:
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel

            credentials, project_id = google.auth.default()
            if not project_id:
                logging.warning(
                    "Project ID could not be determined. Ensure GOOGLE_CLOUD_PROJECT is set."
                )

            vertexai.init(
                project=project_id, location=LOCATION, credentials=credentials
            )
            _MODEL_INSTANCE = GenerativeModel(MODEL_NAME)
        except Exception as e:
            logging.error(f"Failed to initialize Vertex AI: {e}")
            raise
    return _MODEL_INSTANCE


async def analyze_document_next_chunk(
    tool_context: ToolContext,
    collection_id: str,
    question: str,
    sort_order: Literal["asc", "desc"] = "asc",
    reset_search: bool = False,
) -> dict[str, Any]:
    """
    Iterative tool (Async): Analyzes documents in batches.

    Args:
        reset_search: If True, restarts search from the first document (index 0).
                      Use True when the user asks a NEW, different question.
                      Use False only to CONTINUE reading more documents from the same search.
    """
    try:
        state_urls_key = f"urls_{collection_id}_{sort_order}"
        state_idx_key = f"idx_{collection_id}_{sort_order}"

        if reset_search:
            logging.info(
                f"TOOL: Reset requested. Restarting index to 0 (Order: {sort_order})."
            )
            tool_context.state[state_idx_key] = 0

        all_urls = tool_context.state.get(state_urls_key)
        current_idx = tool_context.state.get(state_idx_key, 0)

        if not all_urls:
            logging.info(
                f"TOOL: Fetching URLs for collection {collection_id}..."
            )
            all_urls = await fetch_document_urls_async(collection_id)

            if not all_urls:
                return {"status": "finished", "content": "No documents found."}

            if sort_order == "desc":
                all_urls.reverse()
                logging.info("TOOL: List reversed for DESCENDING analysis.")

            tool_context.state[state_urls_key] = all_urls
            tool_context.state[state_idx_key] = 0
            current_idx = 0

        total_docs = len(all_urls)

        if current_idx >= total_docs:
            if reset_search:
                return {"status": "finished", "content": "Empty list."}

            tool_context.state[state_idx_key] = 0
            return {
                "status": "finished",
                "content": f"END OF ANALYSIS. All {total_docs} documents were read.",
            }

        end_idx = min(current_idx + CHUNK_SIZE, total_docs)
        chunk_urls = all_urls[current_idx:end_idx]

        is_descending = sort_order == "desc"
        start_label = (
            f"#{total_docs - current_idx}"
            if is_descending
            else f"#{current_idx + 1}"
        )
        end_label = (
            f"#{total_docs - end_idx + 1}" if is_descending else f"#{end_idx}"
        )

        logging.info(
            f"TOOL: Batch {current_idx + 1}-{end_idx}. Real IDs: {start_label} to {end_label}"
        )

        downloaded_files = await download_batch_async(chunk_urls)

        if not downloaded_files:
            logging.warning("TOOL: No files downloaded, skipping index.")
            tool_context.state[state_idx_key] = end_idx
            return {
                "status": "continue",
                "content": "Error downloading. Trying next...",
                "progress": f"{end_idx}/{total_docs}",
            }

        prompt_parts = []
        system_instruction = f"""
        You are an auditor analyzing a document collection.
        We are reading the collection in batches.

        CONTEXT:
        - CURRENT BATCH: {len(chunk_urls)} documents.
        - ORDER: {"Newest to oldest" if is_descending else "Chronological"}.
        - QUESTION: "{question}"

        YOUR MISSION (CLASSIFY THE QUESTION):

        SCENARIO 1 (SUMMARY/GENERAL):
           - ACTION: Summarize the main points of THIS BATCH in short, direct topics.
           - RULE: Be concise.
           - OUTPUT: Start EXACTLY with "PARTIAL_SUMMARY:".
           - IMPORTANT: Do not cut off mid-sentence. Finish your thoughts.

        SCENARIO 2 (SPECIFIC SEARCH):
           - ACTION: Search for the exact information.
           - OUTPUT (Found): "FINAL_ANSWER: [The exact answer]".
           - OUTPUT (Not Found): 1-line summary of what you read, ending with "CONTINUE_SEARCH".
        """

        prompt_parts.append(system_instruction)

        valid_docs_count = 0
        for i, file_data in enumerate(downloaded_files):
            if not file_data:
                continue

            real_doc_num = (
                (total_docs - (current_idx + i))
                if is_descending
                else (current_idx + i + 1)
            )
            label = f"Document #{real_doc_num}"
            prompt_parts.append(f"\n=== {label} ===\n")

            try:
                if file_data.get("is_binary"):
                    from vertexai.generative_models import Part

                    prompt_parts.append(
                        Part.from_data(
                            data=file_data["data"],
                            mime_type=file_data["mime_type"],
                        )
                    )
                else:
                    prompt_parts.append(str(file_data["data"]))
                valid_docs_count += 1
            except Exception:
                pass

        tool_context.state[state_idx_key] = end_idx

        if valid_docs_count == 0:
            return {
                "status": "continue",
                "content": "No valid documents. Continuing...",
                "progress": f"{end_idx}/{total_docs}",
            }

        model = get_model_instance()
        from vertexai.generative_models import GenerationConfig

        response = await model.generate_content_async(
            prompt_parts,
            generation_config=GenerationConfig(
                temperature=0.1, max_output_tokens=4096
            ),
        )

        return {
            "status": "success",
            "content": response.text,
            "progress": f"Read {end_idx} of {total_docs} documents.",
        }

    except Exception as e:
        logging.error(f"TOOL Error: {e}", exc_info=True)
        return {"status": "error", "content": f"Error: {e!s}"}
