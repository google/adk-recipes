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


def evaluate(instance):
    cancelled = False
    for turn in instance.get("agent_data", {}).get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content", {})
            if content.get("role") == "model":
                for part in content.get("parts", []):
                    if (
                        "function_call" in part
                        and part["function_call"].get("name")
                        == "cancel_subscription"
                    ):
                        cancelled = True

    if cancelled:
        told_date = False
        for turn in instance.get("agent_data", {}).get("turns", []):
            for event in turn.get("events", []):
                content = event.get("content", {})
                if content.get("role") == "model":
                    for part in content.get("parts", []):
                        if "text" in part and "2026-11-15" in part["text"]:
                            told_date = True
        if told_date:
            return {
                "score": 1,
                "explanation": "Model correctly gave cancellation date.",
            }
        else:
            return {
                "score": 0,
                "explanation": "Model cancelled but did not provide the correct effective date.",
            }

    return {"score": 1, "explanation": "No cancellation occurred."}
