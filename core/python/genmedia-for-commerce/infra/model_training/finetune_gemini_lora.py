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

"""Fine-tune Gemini 3.8 Flash with LoRA for shoe side classification."""

import hashlib
import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.types import CreateTuningJobConfig

sys.stdout.reconfigure(line_buffering=True)

# ── Load config.env ──────────────────────────────────────────────────────────
config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.env")
if os.path.exists(config_path):
    load_dotenv(config_path)

# ── Parameters ──────────────────────────────────────────────────────────────
PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("GENAI_LOCATION", "europe-west4")

NUMBER_OF_EPOCHS = int(os.getenv("FINETUNE_EPOCHS", "10"))
LORA_RANK = int(os.getenv("FINETUNE_LORA_RANK", "2"))  # 2, 4, or 8
LR_MULTIPLIER = float(os.getenv("FINETUNE_LR_MULTIPLIER", "0.5"))
VERSION = int(os.getenv("FINETUNE_VERSION", "1"))
BASE_MODEL = os.getenv(
    "FINETUNE_BASE_MODEL", os.getenv("MODEL_NAME_GENERATED_1")
)

CATALOGUE_SEED = "genmedia_for_commerce_generated_fashion_images"
BUCKET_NAME = hashlib.sha256(CATALOGUE_SEED.encode()).hexdigest()[:63]
DATASET_PREFIX = f"gs://{BUCKET_NAME}/shoe_side_classifier/dataset"
TRAIN_FILE = f"{DATASET_PREFIX}/shoe_side_shoots_train_v3_updated.jsonl"
TEST_FILE = f"{DATASET_PREFIX}/shoe_side_shoots_test_v3_updated.jsonl"

# ── Adapter size mapping ────────────────────────────────────────────────────
ADAPTER_SIZE_MAP = {
    2: "ADAPTER_SIZE_TWO",
    4: "ADAPTER_SIZE_FOUR",
    8: "ADAPTER_SIZE_EIGHT",
}
adapter_size = ADAPTER_SIZE_MAP[LORA_RANK]
tuned_model_display_name = f"classifier_{adapter_size}_lr_{LR_MULTIPLIER}_epochs_{NUMBER_OF_EPOCHS}_v{VERSION}"


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    """Finetune Gemini LoRA model."""
    print(f"Project:    {PROJECT_ID}")
    print(f"Location:   {LOCATION}")
    print(f"Base model: {BASE_MODEL}")
    print(f"LoRA rank:  {LORA_RANK} ({adapter_size})")
    print(f"LR mult:    {LR_MULTIPLIER}")
    print(f"Epochs:     {NUMBER_OF_EPOCHS}")
    print(f"Model name: {tuned_model_display_name}")
    print(f"Train data: {TRAIN_FILE}")
    print(f"Val data:   {TEST_FILE}")
    print()

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

    training_dataset = {"gcs_uri": TRAIN_FILE}
    validation_dataset = types.TuningValidationDataset(gcs_uri=TEST_FILE)

    print("Starting tuning job...")
    tuning_job = client.tunings.tune(
        base_model=BASE_MODEL,
        training_dataset=training_dataset,
        config=CreateTuningJobConfig(
            tuned_model_display_name=tuned_model_display_name,
            adapter_size=adapter_size,
            learningRateMultiplier=LR_MULTIPLIER,
            epochCount=NUMBER_OF_EPOCHS,
            validation_dataset=validation_dataset,
        ),
    )
    print(f"\nTuning job created: {tuning_job.name}")
    print(f"State: {tuning_job.state}")

    # Extract job ID for console URL
    job_name = (
        tuning_job.name
    )  # e.g. projects/123/locations/europe-west4/tuningJobs/456
    job_id = job_name.split("/")[-1] if "/" in job_name else job_name
    console_url = (
        f"https://console.cloud.google.com/vertex-ai/generative/language/tuning/"
        f"{job_id}?project={PROJECT_ID}&region={LOCATION}"
    )
    print("\nFollow training progress here:")
    print(f"  {console_url}")


if __name__ == "__main__":
    main()
