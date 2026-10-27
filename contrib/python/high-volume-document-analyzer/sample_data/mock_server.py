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

import logging

from flask import Flask, jsonify, request

# Configure logging to monitor agent activity
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# 1. Simulate the OAuth (Token) Endpoint
@app.route("/token", methods=["POST"])
def get_token():
    auth = request.headers.get("Authorization")
    logger.info(f"MOCK AUTH: Received token request with Auth: {auth}")

    return jsonify(
        {"access_token": "mock-access-token-12345", "expires_in": 3600}
    )


# 2. Simulate the Document API Endpoint
@app.route("/documents/<collection_id>", methods=["GET"])
def get_documents(collection_id):
    token = request.headers.get("Authorization")
    logger.info(
        f"MOCK API: Fetching collection '{collection_id}' with Token: {token}"
    )

    # Only return documents for collection 12345
    if collection_id == "12345":
        # Returning 15 documents to trigger batching/pagination (Batch size is 10)
        return jsonify(
            [
                f"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf?id={i}"
                for i in range(1, 16)
            ]
        )

    # For any other collection, return empty list
    logger.warning(f"MOCK API: Collection '{collection_id}' not found.")
    return jsonify([])


if __name__ == "__main__":
    print("\n🚀 Mock Server running on http://127.0.0.1:5050")
    print("👉 Collection available: 12345 (15 documents)")
    print("👉 Try in Playground: 'Analyze collection 12345'\n")
    app.run(port=5050)
