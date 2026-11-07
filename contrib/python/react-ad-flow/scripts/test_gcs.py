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

"""Test Google Cloud Storage (GCS) integration for react-ad-flow."""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables
project_dir = Path(__file__).resolve().parent.parent
load_dotenv(project_dir / ".env")


def main() -> int:
    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        print("[FAIL] GCS_BUCKET environment variable is not set.")
        print(
            "Please set it in .env or via: export GCS_BUCKET='your-bucket-name'"
        )
        return 1

    print(f"Testing GCS integration with bucket: gs://{bucket_name}")

    try:
        from google.cloud import storage
    except ImportError:
        print(
            "[FAIL] google-cloud-storage is not installed. Run 'uv sync' first."
        )
        return 1

    try:
        client = storage.Client()
        print("✓ Initialized Google Cloud Storage client")
    except Exception as e:
        print(f"[FAIL] Could not initialize GCS client: {e}")
        return 1

    try:
        bucket = client.bucket(bucket_name)
        if not bucket.exists():
            print(
                f"[FAIL] Bucket gs://{bucket_name} does not exist or access is denied."
            )
            return 1
        print(f"✓ Connected to bucket: gs://{bucket_name}")
    except Exception as e:
        print(f"[FAIL] Error accessing bucket gs://{bucket_name}: {e}")
        return 1

    # Check prefixes
    for prefix in ["seed-images/", "product-images/", "generated/"]:
        blobs = list(bucket.list_blobs(prefix=prefix, max_results=5))
        print(f"  Prefix '{prefix}': found {len(blobs)} item(s)")

    # Test write/read/delete
    test_blob_name = f"test-healthcheck-{int(time.time())}.txt"
    test_payload = b"healthcheck ok"
    blob = bucket.blob(test_blob_name)

    try:
        blob.upload_from_string(test_payload, content_type="text/plain")
        print(f"✓ Uploaded temporary test object: {test_blob_name}")

        downloaded = blob.download_as_bytes()
        if downloaded != test_payload:
            print("[FAIL] Downloaded content did not match uploaded content")
            return 1
        print("✓ Verified test object content matches")

        blob.delete()
        print(f"✓ Deleted temporary test object: {test_blob_name}")
    except Exception as e:
        print(f"[FAIL] Read/write check failed: {e}")
        return 1

    print("\n[SUCCESS] GCS integration is fully operational!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
