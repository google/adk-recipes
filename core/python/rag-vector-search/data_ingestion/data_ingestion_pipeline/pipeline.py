# Copyright 2026 Google LLC
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

from data_ingestion_pipeline.components.ingest_data import ingest_data
from data_ingestion_pipeline.components.process_data import process_data
from kfp import dsl


@dsl.pipeline(
    description="A pipeline to run ingestion of new data into the datastore"
)
def pipeline(
    project_id: str,
    location: str,
    schedule_time: str = "1970-01-01T00:00:00Z",
    is_incremental: bool = True,
    look_back_days: int = 1,
    chunk_size: int = 1500,
    chunk_overlap: int = 20,
    max_rows: int = 100,
    destination_table: str = "incremental_questions_embeddings",
    deduped_table: str = "questions_embeddings",
    destination_dataset: str = "rag_vector_search_qa_data",
    collection_id: str = "",
    ingestion_batch_size: int = 250,
) -> None:
    """Processes data and ingests it into a datastore for RAG Retrieval"""

    # Process the data
    processed_data = process_data(
        project_id=project_id,
        schedule_time=schedule_time,
        is_incremental=is_incremental,
        look_back_days=look_back_days,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        max_rows=max_rows,
        destination_dataset=destination_dataset,
        destination_table=destination_table,
        deduped_table=deduped_table,
        location=location,
    ).set_retry(num_retries=2)

    # Ingest the processed data into Vector Search 2.0 Collection
    ingest_data(
        project_id=project_id,
        location=location,
        collection_id=collection_id,
        input_table=processed_data.output,
        ingestion_batch_size=ingestion_batch_size,
    ).set_retry(num_retries=2)
