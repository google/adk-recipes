#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-api-python-client",
#     "google-auth",
# ]
# ///
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

"""External data source script for Terraform.

Reads JSON from stdin (with project_id, location, collection_id),
outputs JSON to stdout with the data_store_id.

Usage (via Terraform external data source):
  data "external" "data_store_id" {
    program = ["uv", "run", "${path.module}/scripts/get_data_store_id.py"]
    query = {
      project_id    = var.project_id
      location      = var.data_store_region
      collection_id = var.project_name
    }
  }
"""

import json
import sys

import google.auth
from googleapiclient import discovery


def main() -> None:
    query = json.load(sys.stdin)
    project_id = query["project_id"]
    location = query["location"]
    collection_id = query["collection_id"]

    credentials, _ = google.auth.default()
    if location == "global":
        endpoint = "https://discoveryengine.googleapis.com"
    else:
        endpoint = f"https://{location}-discoveryengine.googleapis.com"

    service = discovery.build(
        "discoveryengine",
        "v1alpha",
        credentials=credentials,
        discoveryServiceUrl=f"{endpoint}/$discovery/rest?version=v1alpha",
    )

    connector_name = (
        f"projects/{project_id}/locations/{location}"
        f"/collections/{collection_id}/dataConnector"
    )

    data_store_id = "pending-creation"
    # Default to the queried collection; replaced below with the collection
    # parsed from the data store's actual full resource name.
    resolved_collection_id = collection_id
    try:
        data = (
            service.projects()
            .locations()
            .collections()
            .getDataConnector(name=connector_name)
            .execute()
        )
        entities = data.get("entities", [])
        if entities:
            ds = entities[0].get("dataStore", "")
            parts = ds.split("/")
            if "dataStores" in parts:
                idx = parts.index("dataStores")
                data_store_id = parts[idx + 1]
            if "collections" in parts:
                cidx = parts.index("collections")
                resolved_collection_id = parts[cidx + 1]
    except Exception:  # noqa: S110
        pass

    json.dump(
        {
            "data_store_id": data_store_id,
            "collection_id": resolved_collection_id,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
