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

"""Evaluate all Gemini LoRA fine-tuned endpoints on the validation set.

Lists all endpoints matching the display name pattern, runs inference on each,
computes comprehensive metrics, and updates config.env with the best endpoint.
"""

import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

from dotenv import load_dotenv
from google import genai
from google.cloud import aiplatform, storage
from google.genai import types
from sklearn.metrics import accuracy_score, classification_report, f1_score

sys.stdout.reconfigure(line_buffering=True)

# ── Load config.env ──────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.env")
if os.path.exists(CONFIG_PATH):
    load_dotenv(CONFIG_PATH)

# ── Parameters ──────────────────────────────────────────────────────────────
PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("GENAI_LOCATION", "europe-west4")
DISPLAY_NAME_FILTER = "classifier_"  # match all tuned classifiers

CATALOGUE_SEED = "genmedia_for_commerce_generated_fashion_images"
BUCKET_NAME = hashlib.sha256(CATALOGUE_SEED.encode()).hexdigest()[:63]
TEST_FILE = f"gs://{BUCKET_NAME}/shoe_side_classifier/dataset/shoe_side_shoots_test_v3_updated.jsonl"
MAX_WORKERS = 8

CLASSES = [
    "front",
    "front_right",
    "front_left",
    "top_front",
    "right",
    "left",
    "back",
    "back_right",
    "back_left",
    "sole",
    "invalid",
    "multiple",
]
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}
SIDE_CLASSES = {
    "front",
    "front_right",
    "front_left",
    "top_front",
    "right",
    "left",
    "back",
    "back_right",
    "back_left",
    "sole",
}
SIDE_IDX = {CLASS_TO_IDX[c] for c in SIDE_CLASSES}


def load_test_data(gcs_uri):
    """Parse the test JSONL from GCS to extract image URIs and ground truth labels."""
    # Parse gs://bucket/path
    parts = gcs_uri.replace("gs://", "").split("/", 1)
    bucket_name, blob_path = parts[0], parts[1]

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    content = blob.download_as_text()

    images = []
    labels = []
    for line in content.strip().split("\n"):
        record = json.loads(line)
        contents = record["contents"]
        # User turn has image + text
        user_parts = contents[0]["parts"]
        image_uri = None
        for part in user_parts:
            if "fileData" in part:
                image_uri = part["fileData"]["fileUri"]
                break
        # Model turn has the label
        label = contents[1]["parts"][0]["text"].strip()

        if image_uri and label:
            images.append(image_uri)
            labels.append(label)

    return images, labels


def predict_shoe_position(model, image_uri, client, config):
    """Classify a single image using a tuned endpoint."""
    mime_type = "image/png" if "png" in image_uri else "image/jpeg"
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="classify this product:"),
                types.Part.from_uri(file_uri=image_uri, mime_type=mime_type),
            ],
        )
    ]

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    return response.candidates[0].content.parts[0].text.strip()


def compute_metrics(y_true, y_pred):
    """Compute comprehensive metrics."""
    # Handle predictions not in our class list
    unknown_preds = sum(1 for y in y_pred if y not in CLASS_TO_IDX)

    acc = accuracy_score(y_true, y_pred)
    f1m = f1_score(
        y_true, y_pred, labels=CLASSES, average="macro", zero_division=0
    )
    f1w = f1_score(
        y_true, y_pred, labels=CLASSES, average="weighted", zero_division=0
    )
    errors = int(sum(1 for a, b in zip(y_true, y_pred, strict=False) if a != b))

    # Side-to-side errors
    s2s = sum(
        1
        for i in range(len(y_true))
        if y_true[i] != y_pred[i]
        and y_true[i] in SIDE_CLASSES
        and y_pred[i] in SIDE_CLASSES
    )

    # Invalid F1
    inv_f1 = f1_score(
        y_true, y_pred, labels=["invalid"], average=None, zero_division=0
    )[0]

    # Side misclassified as invalid and vice versa
    side_as_inv = sum(
        1
        for i in range(len(y_true))
        if y_true[i] in SIDE_CLASSES and y_pred[i] == "invalid"
    )
    inv_as_side = sum(
        1
        for i in range(len(y_true))
        if y_true[i] == "invalid" and y_pred[i] in SIDE_CLASSES
    )

    report = classification_report(
        y_true, y_pred, labels=CLASSES, zero_division=0
    )

    return {
        "acc": acc,
        "f1_macro": f1m,
        "f1_weighted": f1w,
        "errors": errors,
        "s2s": s2s,
        "inv_f1": inv_f1,
        "side_as_inv": side_as_inv,
        "inv_as_side": inv_as_side,
        "unknown_preds": unknown_preds,
        "report": report,
    }


def update_config_env(endpoint_name):
    """Update SHOE_CLASSIFICATION_ENDPOINT in config.env with the best endpoint."""
    with open(CONFIG_PATH) as f:
        content = f.read()

    # Replace the existing line (commented or not)
    pattern = r"^#?SHOE_CLASSIFICATION_ENDPOINT=.*$"
    replacement = f"SHOE_CLASSIFICATION_ENDPOINT={endpoint_name}"

    if re.search(pattern, content, re.MULTILINE):
        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        new_content = content.rstrip() + f"\n{replacement}\n"

    with open(CONFIG_PATH, "w") as f:
        f.write(new_content)

    print(f"\nUpdated config.env: SHOE_CLASSIFICATION_ENDPOINT={endpoint_name}")


def main():
    """Evaluate Gemini LoRA model."""
    # Use the system prompt from the codebase
    sys.path.insert(
        0,
        os.path.join(os.path.dirname(__file__), "..", "..", "app"),
    )
    from workflows.spinning.r2v.shoes.classify_shoes import (
        SHOE_CLASSIFICATION_SYSTEM_PROMPT,
    )

    aiplatform.init(project=PROJECT_ID, location=LOCATION)
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

    config = types.GenerateContentConfig(
        temperature=0,
        system_instruction=[
            types.Part.from_text(text=SHOE_CLASSIFICATION_SYSTEM_PROMPT)
        ],
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    # Load test data
    print("Loading test data...")
    images, labels = load_test_data(TEST_FILE)
    print(f"Test samples: {len(images)}")

    # List all endpoints
    print("\nListing endpoints...")
    endpoints = aiplatform.Endpoint.list()
    endpoints = [
        ep for ep in endpoints if DISPLAY_NAME_FILTER in ep.display_name
    ]
    endpoints.sort(key=lambda ep: ep.update_time)

    print(f"Found {len(endpoints)} endpoints matching '{DISPLAY_NAME_FILTER}':")
    for ep in endpoints:
        print(
            f"  {ep.display_name} ({ep.resource_name.split('/')[-1]}) updated={ep.update_time}"
        )

    if not endpoints:
        print("No endpoints found!")
        return

    # Evaluate each endpoint
    all_results = []

    for ep in endpoints:
        endpoint_name = ep.resource_name
        display_name = ep.display_name
        endpoint_id = endpoint_name.split("/")[-1]
        print(f"\n{'=' * 80}")
        print(f"Evaluating: {display_name} (endpoint {endpoint_id})")
        print(f"{'=' * 80}")

        predict_fn = partial(
            predict_shoe_position, endpoint_name, client=client, config=config
        )

        predictions = []
        errors_count = 0
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(predict_fn, img): i
                for i, img in enumerate(images)
            }
            done = 0
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    pred = future.result()
                    predictions.append((idx, pred))
                except Exception as e:
                    predictions.append((idx, "ERROR"))
                    errors_count += 1
                    if errors_count <= 3:
                        print(f"  Error on sample {idx}: {e}")

                done += 1
                if done % 200 == 0:
                    elapsed = time.time() - t0
                    print(f"  {done}/{len(images)} ({elapsed:.0f}s)")

        elapsed = time.time() - t0
        print(
            f"  Inference done: {len(images)} samples in {elapsed:.0f}s ({len(images) / elapsed:.1f} samples/s)"
        )

        if errors_count:
            print(f"  API errors: {errors_count}")

        # Sort by original index
        predictions.sort(key=lambda x: x[0])
        y_pred = [p[1] for p in predictions]

        # Compute metrics
        metrics = compute_metrics(labels, y_pred)
        metrics["endpoint_id"] = endpoint_id
        metrics["display_name"] = display_name
        metrics["endpoint_name"] = endpoint_name
        metrics["inference_time"] = elapsed
        metrics["api_errors"] = errors_count

        print(f"\n  Accuracy:     {metrics['acc']:.4f}")
        print(f"  F1 macro:     {metrics['f1_macro']:.4f}")
        print(f"  F1 weighted:  {metrics['f1_weighted']:.4f}")
        print(f"  Errors:       {metrics['errors']}/{len(labels)}")
        print(f"  S2S errors:   {metrics['s2s']}")
        print(f"  Inv F1:       {metrics['inv_f1']:.4f}")
        print(f"  Side->Inv:    {metrics['side_as_inv']}")
        print(f"  Inv->Side:    {metrics['inv_as_side']}")
        if metrics["unknown_preds"]:
            print(f"  Unknown preds: {metrics['unknown_preds']}")

        print(f"\n{metrics['report']}")

        all_results.append(metrics)

    # Summary: find best by F1 macro
    print(f"\n{'=' * 80}")
    print("SUMMARY — All Endpoints Ranked by F1 Macro")
    print(f"{'=' * 80}")
    all_results.sort(key=lambda x: x["f1_macro"], reverse=True)

    print(
        f"{'Rank':<5} {'Display Name':<55} {'Acc':>7} {'F1mac':>7} {'Errors':>7} "
        f"{'S2S':>5} {'InvF1':>7} {'S->I':>5} {'I->S':>5}"
    )
    print("-" * 110)
    for i, r in enumerate(all_results):
        marker = " <-- BEST" if i == 0 else ""
        print(
            f"{i + 1:<5} {r['display_name']:<55} {r['acc']:>7.4f} {r['f1_macro']:>7.4f} "
            f"{r['errors']:>7} {r['s2s']:>5} {r['inv_f1']:>7.4f} "
            f"{r['side_as_inv']:>5} {r['inv_as_side']:>5}{marker}"
        )

    best = all_results[0]
    print(f"\nBest endpoint: {best['display_name']}")
    print(f"  Endpoint: {best['endpoint_name']}")
    print(f"  F1 macro: {best['f1_macro']:.4f}")
    print(f"  Accuracy: {best['acc']:.4f}")
    print(f"  Errors:   {best['errors']}")

    # Update config.env with best endpoint
    update_config_env(best["endpoint_name"])

    # Save results
    output_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
    save_results = [
        {k: v for k, v in r.items() if k != "report"} for r in all_results
    ]
    with open(output_path, "w") as f:
        json.dump(save_results, f, indent=2)
    print(f"Saved results to {output_path}")


if __name__ == "__main__":
    main()
